"""执行期督办面板（v3.0 第三阶段：从 tasks_page 抽取为独立模块）。

职责边界（与第三页严格区分）：
    第三页「任务督办中心」   下发**之前**的检查、分析、确认、通知预览与模拟下发
    本模块「执行期督办」     下发**之后**的持续督办：分析 → 人工确认/否决 → 受控执行 → 留痕

本模块由第四页「执行反馈与检查」直接调用（`render_execution_period_section`），
不再依赖任何页面模块——因此第四页不再需要导入第三页。

业务保护（全部保留，未做任何放宽）：
    - 动作由规则选出（tools.execution_tool.select_action），大模型只做情况说明；
    - 执行前必须人工确认（confirm），也可人工否决（reject），两者都留痕；
    - 真正的执行走 tools.execution_tool.execute（含黑名单、参数、前置条件、去重、闭环保护）；
    - 本模块不直接写 SQL，所有写入经 database.sqlite_db 的公开接口。

分层约定：模块内先放「业务动作函数」（不依赖 Streamlit，可直接单测），
再放「渲染函数」；两者都不新增业务规则。
"""

from __future__ import annotations

from typing import Any, Mapping

import streamlit as st

from agent import execution_agent
from database import models, sqlite_db
from tools import execution_tool
from ui import labels
from ui.components import cards, state

# ============================================================
# 一、用户可见文案（全部中文，不含任何内部枚举）
# ============================================================

SECTION_EXECUTION: str = "执行期督办分析"
CAPTION_EXECUTION: str = "读取任务与历史事件，由大模型给出情况分析，动作由规则决定"

BUTTON_ANALYZE: str = "开始智能分析"
BUTTON_CONFIRM: str = "确认执行"
BUTTON_CANCEL: str = "取消"

LABEL_SITUATION: str = "当前情况"
LABEL_REASONS: str = "判断依据"
LABEL_PROBLEM: str = "风险与问题"
LABEL_ACTION: str = "建议处理"
LABEL_LIMIT: str = "执行限制"
LABEL_RISK_NOTE: str = "风险提示"
LABEL_OPERATOR: str = "操作人"

HINT_NO_ACTION: str = "该任务当前无需处置：任务已完成、缺少截止时间或进度正常。"
HINT_DRAFT_TAIL: str = "该内容仅作为提醒草稿，不会自动发送。"
HINT_REQUEST_TAIL: str = "系统只提出补充建议，不会自动修改负责人或截止时间。"
HINT_CONFIRM_REQUIRED: str = "执行前请先确认，本模块所有处置动作均需人工授权。"
HINT_IDLE: str = "尚未生成处置建议。点击上方「开始智能分析」由系统读取任务与历史事件后给出建议。"

MSG_ANALYSIS_FAILED: str = "智能分析暂时失败，请稍后重试。"
# 与第三页 tasks_page.MSG_DB_CONFLICT 同一文案：两处均为页面级提示常量，
# 各自独立定义以避免页面之间互相导入（模块解耦优先于字面去重）。
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

# 折叠区标题与说明（由第四页展示）
EXPANDER_TITLE: str = "执行期处置（提醒草稿 / 提级督办，需人工确认）"
EXPANDER_CAPTION: str = (
    "针对当前选中任务，由规则选出动作、模型仅给出情况说明，执行前必须人工确认；"
    "已正式闭环的任务会被拒绝处置。"
)


def ensure_sentence_end(text: str) -> str:
    """确保提示语以单个句末标点结尾，避免与业务服务返回的原因重复追加句号。

    纯函数，便于单测：
    - 已以 。！？.!?;； 结尾 → 原样返回（保留业务服务的完整原因）；
    - 否则补一个中文句号。
    """
    value = str(text or "").strip()
    if not value:
        return value
    if value[-1] in "。！？!?.;；":
        return value
    return value + "。"


# ============================================================
# 二、业务动作（不依赖 Streamlit，可直接单测）
# ============================================================


def run_analysis(
    task_id: int,
    *,
    llm_fn: Any = None,
    db_path: Any = None,
    today: Any = None,
) -> dict[str, Any]:
    """对单个任务做**执行期**处置分析，返回统一结构。

    与「下发前分析」（agent.execution_agent.analyze_before_dispatch）的区别：
        下发前分析：任何待下发任务都检查「清不清楚、能不能下发」（与提醒等级无关）；
        执行期分析：只在临期/逾期时判断「要不要催办」，受提醒等级驱动。

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
        # Validator 拒绝的是「建议动作」，不是「分析本身」。
        # 分析结论（情况说明 / 判断依据 / 风险提示）必须保留，交由界面展示；
        # 仅把动作标记为「当前被规则限制」，不提供执行入口。
        blocked_plan = dict(result)
        blocked_plan["blocked"] = True
        blocked_plan["block_reason"] = str(result.get("error") or "")
        return {
            "ok": False,
            "kind": "validator_rejected",
            "message": MSG_VALIDATOR_REJECTED,
            "plan": blocked_plan,
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
    此外 L4 与 execute 的闭环闸门共同保证：已正式闭环任务不会被处置。
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
    except sqlite_db.ConcurrentUpdateError:
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
# 三、渲染（第四页调用）
# ============================================================


def render_execution_period_section(
    task: Mapping[str, Any], task_id: int, scope: str = "inspection"
) -> None:
    """执行期督办入口（第四页「执行反馈与检查」调用）。

    这是「下发之后」的处置入口，与第三页的下发前分析是两件事，
    因此在这里自带折叠区与说明，避免与下发前检查混淆。
    """
    with st.expander(EXPANDER_TITLE, expanded=False):
        st.caption(EXPANDER_CAPTION)
        render_execution_panel(task, task_id, scope=scope)


def render_execution_panel(
    task: Mapping[str, Any],
    task_id: int,
    scope: str = "inspection",
) -> None:
    """执行期督办分析区：发起分析 → 展示建议 → 人工确认或取消。

    scope 只用于区分 widget key，避免同一任务在不同位置渲染时 key 冲突。
    """
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
        outcome_plan = outcome.get("plan")
        if outcome["ok"]:
            state.save_execution_plan(task_id, outcome_plan)
        elif (
            outcome["kind"] == "validator_rejected"
            and isinstance(outcome_plan, Mapping)
            and (outcome_plan.get("situation") or outcome_plan.get("reasons"))
        ):
            # 分析已产出结论，只是建议动作被规则限制：
            # 保留结论供展示（下方会给出受限原因），不提供执行入口。
            state.save_execution_plan(task_id, dict(outcome_plan))
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
        st.caption(HINT_IDLE)
        return

    # 分区展示分析结论：当前情况 → 判断依据 → 风险与问题 → 建议处理 → 执行限制
    st.markdown(f"**{LABEL_SITUATION}**")
    st.write(str(plan.get("situation") or "—"))

    st.markdown(f"**{LABEL_REASONS}**")
    cards.bullet_list([str(item) for item in (plan.get("reasons") or [])])

    st.markdown(f"**{LABEL_PROBLEM}**")
    st.write(str(plan.get("risk_note") or "—"))

    action_text = labels.action_label(plan.get("action"))
    st.markdown(f"**{LABEL_ACTION}**")
    st.write(action_text)

    if plan.get("blocked"):
        # 执行限制：动作被规则限制时，展示具体原因，且不提供被禁止的执行入口。
        st.markdown(f"**{LABEL_LIMIT}**")
        st.warning(
            f"分析已完成。但建议动作「{action_text}」当前不满足执行条件，系统未提供执行入口。"
        )
        st.caption(
            "限制原因："
            + ensure_sentence_end(str(plan.get("block_reason") or "当前规则不允许执行该动作"))
        )
        return

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
    elif tone == "warning":
        st.warning(message)
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
