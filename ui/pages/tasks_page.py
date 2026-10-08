"""页面四：任务督办中心。

- 数据来源：database.sqlite_db.list_tasks()（支持状态 / 部门筛选）
- 状态修改：database.sqlite_db.update_status()（自动写入操作日志）
- 日志查看：database.sqlite_db.get_logs()
- 智能处置（v2.0）：agent.execution_agent.analyze() → 人工确认/取消
  → tools.execution_tool.execute() → 事务事件记忆
优先级与风险等级、负责人关键词为展示层筛选（不改变业务接口）。

分层约定：本页只做「渲染 + 编排」，所有可测的业务动作都放在下面的
`run_analysis` / `confirm_execution` / `reject_execution` 三个函数里，
它们不依赖 Streamlit 运行时，便于直接单元测试。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

import streamlit as st

from agent import execution_agent, reminder_agent
from database import models, sqlite_db
from tools import execution_tool
from ui import labels
from ui.components import cards, state, styles, tables
from ui.pages import submission_view

# 页面标题与副标题
PAGE_TITLE: str = "任务督办中心"
PAGE_SUBTITLE: str = "查看任务、更新状态、智能处置、全程留痕"

# 筛选下拉的「全部」选项
ALL_OPTION: str = "全部"

# ============================================================
# 智能处置：用户可见文案（全部中文，不含任何内部枚举）
# ============================================================

SECTION_FOCUS: str = "当前处置任务"
CAPTION_FOCUS: str = "由首页「AI 主动发现」跳转而来，可直接执行智能处置"
BUTTON_BACK_ALL: str = "查看全部任务"
HINT_FOCUS_CONFIRM: str = "需人工确认"

SECTION_EXECUTION: str = "智能处置分析"
CAPTION_EXECUTION: str = "读取任务与历史事件，由大模型给出情况分析，动作由规则决定"

BUTTON_ANALYZE: str = "开始智能分析"
BUTTON_CONFIRM: str = "确认执行"
BUTTON_CANCEL: str = "取消"

LABEL_SITUATION: str = "当前情况"
LABEL_REASONS: str = "判断依据"
LABEL_ACTION: str = "建议处理"
LABEL_RISK_NOTE: str = "风险提示"
LABEL_OPERATOR: str = "操作人"

HINT_NO_ACTION: str = "该任务当前无需处置：任务已完成、缺少截止时间或进度正常。"
HINT_DRAFT_TAIL: str = "该内容仅作为提醒草稿，不会自动发送。"
HINT_REQUEST_TAIL: str = "系统只提出补充建议，不会自动修改负责人或截止时间。"
HINT_CONFIRM_REQUIRED: str = "执行前请先确认，本页所有处置动作均需人工授权。"

MSG_ANALYSIS_FAILED: str = "智能分析暂时失败，请稍后重试。"
MSG_DB_CONFLICT: str = "任务状态刚刚发生变化，本次操作未执行，请重新分析后再试。"
MSG_VALIDATOR_REJECTED: str = "当前任务条件不满足该处理操作，系统已阻止执行。"
MSG_STALE_PLAN: str = "任务状态已发生变化，请重新分析。"
MSG_EXECUTE_FAILED: str = "智能处置执行失败，请稍后重试。"
MSG_REJECTED: str = "已取消本次处置建议，系统已记录人工否决。"

# 执行成功后的提示语（按动作区分，均说明真实发生了什么）
RESULT_MESSAGES: dict[str, str] = {
    execution_tool.ACTION_DRAFT_NOTIFY: "提醒草稿已生成。",
    execution_tool.ACTION_REQUEST_INFO: "已记录补充信息请求。",
    execution_tool.ACTION_ESCALATE: "已提级督办，风险等级已提升为高风险。",
}


# ============================================================
# 智能处置：业务动作（不依赖 Streamlit，可直接单测）
# ============================================================


def run_analysis(
    task_id: int,
    *,
    llm_fn: Any = None,
    db_path: Any = None,
    today: Any = None,
) -> dict[str, Any]:
    """调用 ExecutionAgent 分析单个任务，返回统一结构。

    返回：
        {"ok": bool, "kind": str, "message": str, "plan": dict | None}

    kind 取值：plan（可确认）/ no_action（无需处置）/
              analysis_error / db_error / validator_rejected / unexpected

    错误处理原则：**绝不向用户暴露 traceback**，所有异常都转成中文提示。
    """
    try:
        result = execution_agent.analyze(task_id, llm_fn=llm_fn, db_path=db_path, today=today)
    except execution_agent.ExecutionAnalysisError:
        return {"ok": False, "kind": "analysis_error", "message": MSG_ANALYSIS_FAILED, "plan": None}
    except sqlite_db.DatabaseError:
        return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "plan": None}
    except Exception:
        return {"ok": False, "kind": "unexpected", "message": MSG_EXECUTE_FAILED, "plan": None}

    if result is None:
        return {"ok": False, "kind": "no_action", "message": HINT_NO_ACTION, "plan": None}

    if not result.get("ok"):
        # Validator 拒绝了：不是系统故障，如实告知「条件不满足」
        return {
            "ok": False,
            "kind": "validator_rejected",
            "message": MSG_VALIDATOR_REJECTED,
            "plan": result,
        }

    return {"ok": True, "kind": "plan", "message": "", "plan": result}


def confirm_execution(
    plan: Mapping[str, Any],
    *,
    operator: str = models.DEFAULT_OPERATOR,
    db_path: Any = None,
    today: Any = None,
) -> dict[str, Any]:
    """人工确认后执行处置计划。

    执行顺序（不可颠倒）：
        重新读取任务与历史 → 重新走 Validator → 写 confirm → 执行 → 写动作事件

    「重新走 Validator」是防重复执行的关键：Streamlit 的 rerun 可能让同一个
    计划被点两次，靠 session_state 里的旧计划是不可靠的，必须以数据库当前状态
    为准。Validator 的 L6（近期同动作去重）与 L4（前置条件）已经能拦住：
        DRAFT_NOTIFY  → 近期已有 draft        → 拒绝
        REQUEST_INFO  → 近期已有 request       → 拒绝
        ESCALATE      → risk_level 已是 red    → 拒绝
    """
    try:
        task_id = int(plan["task_id"])
        action = str(plan.get("action") or "")
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "kind": "unexpected", "message": MSG_EXECUTE_FAILED, "result": None}

    # ---------- 1. 以数据库当前状态为准，重新校验 ----------
    try:
        task = sqlite_db.get_task(task_id, db_path=db_path)
        if task is None:
            return {"ok": False, "kind": "stale", "message": MSG_STALE_PLAN, "result": None}
        logs = sqlite_db.get_logs(task_id, db_path=db_path)
        validation = execution_tool.validate_plan(task, dict(plan), logs, today=today)
    except sqlite_db.DatabaseError:
        return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "result": None}

    if not validation.ok:
        return {"ok": False, "kind": "stale", "message": MSG_STALE_PLAN, "result": None}

    # ---------- 2. 写 confirm（人工授权留痕）----------
    try:
        sqlite_db.append_log(
            task_id,
            action=models.ACTION_CONFIRM,
            operator=operator,
            remark=execution_tool.build_remark(
                action, plan.get("level"), plan.get("days_left"), plan.get("situation", "")
            ),
            db_path=db_path,
        )
    except sqlite_db.DatabaseError:
        return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "result": None}

    # ---------- 3. 受控执行 ----------
    try:
        outcome = execution_tool.execute(
            action,
            task=task,
            params=plan.get("params") or {},
            operator=operator,
            remark=execution_tool.build_remark(
                action, plan.get("level"), plan.get("days_left"), plan.get("situation", "")
            ),
            today=today,
            db_path=db_path,
        )
    except execution_tool.ConcurrentUpdateError:
        return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "result": None}
    except sqlite_db.DatabaseError:
        return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "result": None}
    except ValueError:
        return {"ok": False, "kind": "validator_rejected", "message": MSG_VALIDATOR_REJECTED, "result": None}
    except Exception:
        return {"ok": False, "kind": "unexpected", "message": MSG_EXECUTE_FAILED, "result": None}

    # ---------- 4. 补写动作事件 ----------
    # ESCALATE 的 escalate 日志由 update_risk_level 在同一事务内自动写入；
    # DRAFT_NOTIFY / REQUEST_INFO 不改数据，其事件由本层补记。
    event = execution_tool.ACTION_LOG_EVENT.get(action)
    if event and event != models.ACTION_ESCALATE:
        try:
            sqlite_db.append_log(
                task_id,
                action=event,
                operator=operator,
                remark=execution_tool.build_remark(
                    action, plan.get("level"), plan.get("days_left"), outcome.get("message", "")
                ),
                db_path=db_path,
            )
        except sqlite_db.DatabaseError:
            return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "result": None}

    return {
        "ok": True,
        "kind": action,
        "message": RESULT_MESSAGES.get(action, ""),
        "result": outcome,
    }


def reject_execution(
    plan: Mapping[str, Any],
    *,
    operator: str = models.DEFAULT_OPERATOR,
    db_path: Any = None,
) -> dict[str, Any]:
    """人工取消本次处置建议，写入 reject 事件。

    被否决的事实必须留痕：否则任务会被反复提出同一份建议，
    且事后无法解释「为什么这条一直没被处理」。
    """
    try:
        task_id = int(plan["task_id"])
        action = str(plan.get("action") or "")
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "kind": "unexpected", "message": MSG_EXECUTE_FAILED, "result": None}

    try:
        sqlite_db.append_log(
            task_id,
            action=models.ACTION_REJECT,
            operator=operator,
            remark=f"人工否决本次处置建议（{labels.action_label(action)}）",
            db_path=db_path,
        )
    except sqlite_db.DatabaseError:
        return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "result": None}

    return {"ok": True, "kind": "rejected", "message": MSG_REJECTED, "result": None}


# ============================================================
# 页面渲染
# ============================================================


def render() -> None:
    """渲染任务督办中心。"""
    cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)

    flash = state.pop_flash()
    if flash:
        st.success(flash)

    # 首页「查看并处理」的定位任务：先消费，使「当前处置任务」成为第一视觉焦点。
    # 消费后即失效（pop 语义），不会在刷新后反复跳转。
    focus_id = state.pop_focus_task()

    all_tasks = sqlite_db.list_tasks()
    if not all_tasks:
        if cards.empty_state(
            "暂无任务记录",
            "请先上传会议纪要或任务台账，识别确认后写入库。",
            "前往会议材料上传",
        ):
            state.goto("会议材料上传")
        return

    focus_task = _find_task(all_tasks, focus_id)
    if focus_task is not None:
        _render_focus_panel(focus_task)
        st.divider()

    filters = _render_filters(all_tasks)
    filtered = _apply_filters(filters)

    cards.section(
        f"全部任务（{len(filtered)} / {len(all_tasks)} 条）",
        "高风险浅红、中风险浅黄、低风险白底",
    )
    if not filtered:
        st.warning("当前筛选条件下没有任务。")
        return

    st.dataframe(tables.tasks_styler(filtered), hide_index=True, use_container_width=True)

    st.divider()
    _render_actions(filtered, preselect_id=focus_id)


# ------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------


def _render_filters(all_tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """渲染筛选区，返回筛选条件。"""
    departments = sorted({str(task["department"]) for task in all_tasks if task.get("department")})

    status_col, department_col, owner_col = st.columns([1, 1, 1])
    status_label = status_col.selectbox("状态", [ALL_OPTION, *tables.status_options()])
    department_label = department_col.selectbox("部门", [ALL_OPTION, *departments])
    owner_keyword = owner_col.text_input("负责人（关键词）", placeholder="支持模糊匹配")

    priority_col, risk_col = st.columns([1, 1])
    priority_label = priority_col.selectbox("优先级", [ALL_OPTION, *tables.priority_options()])
    risk_label = risk_col.selectbox("风险等级", [ALL_OPTION, *tables.risk_options()])

    return {
        "status": tables.status_options().get(status_label),
        "department": None if department_label == ALL_OPTION else department_label,
        "owner": owner_keyword.strip(),
        "priority": tables.priority_options().get(priority_label),
        "risk": tables.risk_options().get(risk_label),
    }


def _apply_filters(filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    """状态 / 部门下推到 list_tasks()，其余条件在展示层过滤。"""
    tasks = sqlite_db.list_tasks(status=filters["status"], department=filters["department"])

    owner_keyword = str(filters["owner"] or "")
    if owner_keyword:
        tasks = [task for task in tasks if owner_keyword in str(task.get("owner") or "")]

    priority = filters["priority"]
    if priority:
        tasks = [task for task in tasks if task.get("priority") == priority]

    risk = filters["risk"]
    if risk:
        tasks = [task for task in tasks if task.get("risk_level") == risk]

    return tasks


def _find_task(
    tasks: Sequence[Mapping[str, Any]],
    task_id: int | None,
) -> Mapping[str, Any] | None:
    """在任务集合中定位目标任务；找不到返回 None（不报错、不阻断页面）。"""
    if task_id is None:
        return None
    for task in tasks:
        try:
            if int(task.get("id")) == int(task_id):
                return task
        except (TypeError, ValueError):
            continue
    return None


def _render_focus_panel(task: Mapping[str, Any]) -> None:
    """当前处置任务：从首页「查看并处理」跳转过来时的第一视觉焦点。

    只做展示与既有能力的复用：
    - 任务详情复用 _render_detail（含状态/负责人/部门/截止/提醒等级/风险/优先级）；
    - 智能处置复用 _render_execution_panel（开始智能分析 / 人工确认）；
    不新增任何业务判断，也不调用大模型。
    """
    task_id = int(task["id"])
    cards.section(SECTION_FOCUS, CAPTION_FOCUS)

    detail_col, side_col = st.columns([11, 9], gap="large")

    with detail_col:
        _render_detail(task)

    with side_col:
        confirm_text = HINT_FOCUS_CONFIRM if task.get("need_confirm") else "无需确认"
        st.markdown(
            f'<div class="cf-card"><div class="cf-file-head" '
            f'style="color:{styles.TEXT_MAIN}"><span>人工确认状态</span></div>'
            f'<div class="cf-file-row"><span class="cf-file-label">当前状态</span>'
            f'<span>{tables.status_label(task.get("status"))}</span></div>'
            f'<div class="cf-file-row"><span class="cf-file-label">人工确认</span>'
            f'<span>{confirm_text}</span></div></div>',
            unsafe_allow_html=True,
        )

    st.write("")
    _render_execution_panel(task, task_id, scope="focus")


def _render_actions(
    tasks: Sequence[Mapping[str, Any]],
    preselect_id: int | None = None,
) -> None:
    """任务操作区。

    两种模式：
    - **普通管理模式**（直接从导航进入，preselect_id 为 None）：
      完整渲染「任务状态修改 + 详情 + 智能处置 + 操作日志」，行为与改造前一致。
    - **专注模式**（首页「查看并处理」跳转，preselect_id 非空）：
      目标任务已在页面顶部「当前处置任务」区完整呈现（含详情、人工确认状态与
      「开始智能分析」），此处**只保留任务切换入口**，不再重复渲染同一任务的
      详情与智能处置，避免出现两个任务详情、两个「开始智能分析」。
      用户切换到其他任务后，才恢复完整的操作区。
    """
    options = {tables.task_option_label(task): task for task in tasks}
    option_list = list(options)

    focus_index = 0
    if preselect_id is not None:
        for i, opt in enumerate(option_list):
            if int(options[opt]["id"]) == int(preselect_id):
                focus_index = i
                break

    # 专注模式：先给「切换任务」区块标题，再放选择器。
    # Streamlit 自上而下渲染，若把标题放在选择器之后，会出现
    # 「选择器悬在区块标题之上」的错位观感（标题看起来属于下一块）。
    if preselect_id is not None:
        cards.section(
            "切换任务",
            "当前处置任务已在页面顶部展示；选择其他任务即可进行状态修改与智能处置",
        )

    selected_label = st.selectbox("选择任务", option_list, index=focus_index)
    task = options[selected_label]
    task_id = int(task["id"])

    # 仍停在 focus 任务：顶部「当前处置任务」已完整呈现，不再重复渲染
    if preselect_id is not None and task_id == int(preselect_id):
        return

    # 切到其他任务（或普通模式）-> 进入完整管理模式
    cards.section("任务状态修改", "状态变更会自动写入操作日志")

    detail_col, form_col = st.columns([11, 9], gap="large")

    with detail_col:
        _render_detail(task)

    with form_col:
        _render_status_form(task, task_id)

    st.write("")
    submission_view.render_entry(task, task_id, scope="list")

    st.write("")
    _render_execution_panel(task, task_id, scope="list")

    st.write("")
    cards.section("操作日志", "状态变更、下发与智能处置全程留痕")
    logs = sqlite_db.get_logs(task_id)
    if logs:
        st.dataframe(tables.logs_dataframe(logs), hide_index=True, use_container_width=True)
    else:
        st.caption("该任务暂无操作日志。")


def _render_execution_panel(
    task: Mapping[str, Any],
    task_id: int,
    scope: str = "list",
) -> None:
    """智能处置分析区：发起分析 → 展示建议 → 人工确认或取消。"""
    cards.section(SECTION_EXECUTION, CAPTION_EXECUTION)

    operator = st.text_input(
        LABEL_OPERATOR, value=models.DEFAULT_OPERATOR, key=f"cf_exec_{scope}_operator_{task_id}"
    )

    result = state.get_execution_result()
    if result and int(result.get("task_id") or 0) == task_id:
        _render_execution_result(result)

    if st.button(BUTTON_ANALYZE, key=f"cf_exec_{scope}_analyze_{task_id}"):
        outcome = run_analysis(task_id)
        state.clear_execution_result()
        if outcome["ok"]:
            state.save_execution_plan(task_id, outcome["plan"])
        else:
            state.clear_execution_plan()
            state.save_execution_result(
                {
                    "task_id": task_id,
                    "tone": "info" if outcome["kind"] == "no_action" else "error",
                    "message": outcome["message"],
                }
            )
        st.rerun()

    plan = state.get_execution_plan(task_id)
    if not plan:
        st.caption("尚未生成处置建议。点击上方「开始智能分析」由系统读取任务与历史事件后给出建议。")
        return

    st.markdown(f"**{LABEL_SITUATION}**")
    st.write(str(plan.get("situation") or "—"))

    st.markdown(f"**{LABEL_REASONS}**")
    cards.bullet_list([str(item) for item in (plan.get("reasons") or [])])

    action_text = labels.action_label(plan.get("action"))
    st.markdown(f"**{LABEL_ACTION}**")
    st.write(action_text)

    st.markdown(f"**{LABEL_RISK_NOTE}**")
    st.write(str(plan.get("risk_note") or "—"))

    st.caption(HINT_CONFIRM_REQUIRED)

    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        confirmed = st.button(
            BUTTON_CONFIRM,
            key=f"cf_exec_{scope}_confirm_{task_id}",
            type="primary",
            use_container_width=True,
        )
    with cancel_col:
        cancelled = st.button(
            BUTTON_CANCEL, key=f"cf_exec_{scope}_cancel_{task_id}", use_container_width=True
        )

    if confirmed:
        outcome = confirm_execution(plan, operator=operator.strip() or models.DEFAULT_OPERATOR)
        _after_decision(task_id, outcome, action_text)
        st.rerun()

    if cancelled:
        outcome = reject_execution(plan, operator=operator.strip() or models.DEFAULT_OPERATOR)
        _after_decision(task_id, outcome, action_text)
        st.rerun()


def _after_decision(task_id: int, outcome: Mapping[str, Any], action_text: str) -> None:
    """确认/取消之后统一收尾：落结果、清计划。"""
    tone = "success" if outcome.get("ok") else "error"
    state.save_execution_result(
        {
            "task_id": task_id,
            "tone": tone,
            "message": str(outcome.get("message") or ""),
            "action_text": action_text,
            "action": str(outcome.get("kind") or ""),
            "result": outcome.get("result"),
        }
    )
    # 计划已消费，必须清空：否则刷新后还能拿旧计划再执行一次
    state.clear_execution_plan()


def _render_execution_result(result: Mapping[str, Any]) -> None:
    """展示上一次的执行结果（成功 / 失败 / 无需处置）。"""
    tone = str(result.get("tone") or "info")
    message = str(result.get("message") or "")

    if tone == "success":
        st.success(message)
    elif tone == "error":
        st.error(message)
    else:
        st.info(message)

    action = str(result.get("action") or "")
    payload = result.get("result")
    if not isinstance(payload, Mapping):
        return

    if action == execution_tool.ACTION_DRAFT_NOTIFY:
        cards.hint([str(payload.get("message") or ""), HINT_DRAFT_TAIL])
        return

    if action == execution_tool.ACTION_REQUEST_INFO:
        cards.hint([str(payload.get("message") or ""), HINT_REQUEST_TAIL])


def _reminder_level_text(deadline: Any) -> str:
    """按截止时间算出提醒等级的中文名（复用 ReminderAgent，不另建规则）。"""
    days_left = reminder_agent.compute_days_left(deadline, date.today())
    if days_left is None:
        return "无法判定"
    return labels.level_label(reminder_agent.compute_level(days_left))


def _render_detail(task: Mapping[str, Any]) -> None:
    """任务详情卡片（含中文风险徽章与提醒等级）。"""
    risk = str(task.get("risk_level") or "")
    badge = styles.risk_badge_html(risk)

    rows = [
        ("当前状态", tables.status_label(task.get("status"))),
        ("负责人", task.get("owner") or "未指定"),
        ("责任部门", task.get("department") or "未指定"),
        ("截止时间", task.get("deadline") or "未指定"),
        ("提醒等级", _reminder_level_text(task.get("deadline"))),
        ("风险等级", tables.risk_label(task.get("risk_level"))),
        ("优先级", tables.priority_label(task.get("priority"))),
    ]
    body = "".join(
        f'<div class="cf-file-row"><span class="cf-file-label">{label}</span><span>{value}</span></div>'
        for label, value in rows
    )
    st.markdown(
        f'<div class="cf-card"><div class="cf-file-head" style="color:{styles.TEXT_MAIN}">'
        f"<span>任务详情</span>{badge}</div>{body}</div>",
        unsafe_allow_html=True,
    )


def _render_status_form(task: Mapping[str, Any], task_id: int) -> None:
    """状态修改表单（提交后调用 update_status 并留痕）。"""
    status_map = tables.status_options()
    # 局部变量刻意不叫 labels：避免遮蔽模块级 from ui import labels
    status_labels = list(status_map)
    current = tables.status_label(task.get("status"))
    index = status_labels.index(current) if current in status_labels else 0

    with st.form(key=f"cf_status_form_{task_id}"):
        new_label = st.selectbox("新状态", status_labels, index=index)
        operator = st.text_input("操作人", value="AI")
        remark = st.text_input("备注", value="")
        submitted = st.form_submit_button("更新任务状态", type="primary", use_container_width=True)

        if submitted:
            try:
                sqlite_db.update_status(
                    task_id,
                    status_map[new_label],
                    operator=operator.strip() or "AI",
                    remark=remark.strip(),
                )
            except (ValueError, sqlite_db.TaskNotFoundError) as exc:
                st.error(f"状态更新失败：{exc}")
            else:
                state.set_flash(f"任务 #{task_id} 状态已更新为「{new_label}」。")
                st.rerun()
