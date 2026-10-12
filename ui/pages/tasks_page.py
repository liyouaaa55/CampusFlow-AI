"""页面三：任务督办中心（下发前流程）。

职责边界（v3.0 第三阶段解耦后）：
- 本页只负责**下发之前**：任务查看/筛选 → 任务信息修正 → 任务确认
  → 下发前智能分析 → 通知预览 → 系统内模拟下发 → 发送记录。
- **下发之后**的执行期督办（分析 / 确认 / 否决 / 执行）已抽取到独立模块
  `ui/pages/execution_panel.py`，由第四页「执行反馈与检查」直接调用，本页不再承担。

数据来源：database.sqlite_db.list_tasks()（支持状态 / 部门筛选）
状态修改：database.sqlite_db.update_status()（自动写入操作日志）
日志查看：database.sqlite_db.get_logs()

分层约定：本页只做「渲染 + 编排」，可测的业务动作放在
`run_predispatch_analysis` / `save_task_info` / `confirm_task` 三个函数里，
它们不依赖 Streamlit 运行时，便于直接单元测试。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

import streamlit as st

from agent import execution_agent, reminder_agent
from database import models, sqlite_db
from ui import labels
from ui.components import cards, state, styles, tables
from ui.pages import nav, submission_view

# 页面标题与副标题
PAGE_TITLE: str = "任务督办中心"
PAGE_SUBTITLE: str = "修正任务信息、执行下发前智能分析、预览通知并完成系统内模拟下发"

# 筛选下拉的「全部」选项
ALL_OPTION: str = "全部"

# ============================================================
# 智能处置：用户可见文案（全部中文，不含任何内部枚举）
# ============================================================

SECTION_FOCUS: str = "当前处置任务"
CAPTION_FOCUS: str = "由首页「AI 主动发现」跳转而来，可直接推进下发前流程"
BUTTON_BACK_ALL: str = "查看全部任务"
HINT_FOCUS_CONFIRM: str = "需人工确认"

# --- 任务概览（只做展示层计数，不派生业务字段、不计算完成率）---
OVERVIEW_TITLE: str = "任务概览"
OVERVIEW_CAPTION: str = "统计口径：按任务当前状态字段与风险等级实时计数，不含闭环口径"
RISK_OVERVIEW_TITLE: str = "风险概览"
RISK_DISTRIBUTION_TITLE: str = "风险分布"
OVERVIEW_CONFIRM_HINT: str = "任务需人工确认后方可下发"

# 与第四页执行期督办模块（ui/pages/execution_panel.py）中的同名常量内容一致：
# 两处都是页面级提示文案，各自独立定义，避免页面模块之间互相导入。
MSG_DB_CONFLICT: str = "任务状态刚刚发生变化，本次操作未执行，请重新分析后再试。"

# ============================================================
# 任务处置工作区（详情 → 任务确认 → 下发 → 智能处置）
# ============================================================

WORKSPACE_TITLE: str = "任务处置工作区"
WORKSPACE_CAPTION: str = "任务详情、任务确认、下发前智能分析与系统内下发在同一工作区按流程自上而下推进"

# 任务列表固定高度：避免大量任务把整页无限撑长（超出部分由表格内部滚动）
TASK_TABLE_HEIGHT: int = 320

# ============================================================
# 任务视图与选择（v3.0 第四阶段：最新任务优先 + 本次任务定位）
# ============================================================
#
# 三条纪律：
#     1. 排序——页面展示按「最新创建优先」，数据库默认排序契约不变；
#     2. 定位——默认选中优先级为 focus（入库/首页跳转指定）> 本次入库任务 > 列表第一条；
#     3. 诚实——「本次」只依据 batch_create() 真实返回的 ID，且仅存活于当前会话，
#        没有可靠 ID 时明确提示，绝不把历史任务伪装成本次任务。

VIEW_BATCH: str = "本次生成的任务"
VIEW_ALL: str = "全部任务"
KEY_TASK_VIEW: str = "cf_tasks_view"          # 视图切换控件的稳定 key
KEY_TASK_SELECT: str = "cf_tasks_selected"    # 任务选择器的稳定 key

LBL_TASK_VIEW: str = "任务视图"
HINT_TASK_LIST_TITLE: str = (
    "高风险浅红、中风险浅黄、低风险白底；表格固定高度，可内部滚动，不撑长整页"
)
CAPTION_VIEW: str = (
    "「本次生成的任务」只依据本次确认入库时数据库真实返回的任务 ID 筛选；"
    "该记录仅存活于当前会话，系统未持久化材料来源"
)
CAPTION_VIEW_NO_BATCH: str = (
    "本次会话尚无「确认入库」记录，因此只提供「全部任务」视图；"
    "系统未持久化材料来源，不按标题或时间推测本次任务"
)
NOTE_BATCH_FILTERED_OUT: str = "本次生成的任务被当前筛选条件排除，请把筛选恢复为「全部」后查看。"
NOTE_BATCH_MISSING: str = "本次会话记录的本次任务在数据库中已不存在（可能已被删除）。"
NOTE_FOCUS_NOT_VISIBLE: str = (
    "需定位的任务不在当前筛选结果中，已按当前筛选显示；如需查看请把筛选恢复为「全部」。"
)
NOTE_BATCH_SELECTED: str = "已按本次入库任务定位。"

# --- 任务确认（正式下发的前置条件，必须始终可见）---
CONFIRM_SECTION: str = "任务确认"
CONFIRM_CAPTION: str = "任务确认是正式下发的前置条件；它与「办结标准已确认」是两件不同的事"
HINT_TASK_PENDING_CONFIRM: str = "任务待确认"
HINT_TASK_CONFIRM_SATISFIED: str = "任务已满足确认要求"
BTN_CONFIRM_TASK: str = "确认任务"
LBL_CONFIRM_OPERATOR: str = "确认人"
MSG_TASK_CONFIRMED: str = "任务已确认，可继续推进下发。"
MSG_TASK_CONFIRM_FAILED: str = "任务确认失败，请稍后重试。"
HINT_CONFIRM_BLOCKS_DISPATCH: str = (
    "未确认的任务不能生成下发通知，也不能在系统内下发。请先在此完成确认。"
)
HINT_CONFIRM_NOT_CRITERIA: str = (
    "注意：「办结标准已确认」只代表闭环依据就绪，不等于「任务已确认」。"
)
HINT_CONFIRM_NO_FORM: str = "无需再次确认：该任务已满足下发的前置确认要求。"


# ============================================================
# 下发前流程：业务动作（不依赖 Streamlit，可直接单测）
# ============================================================


def needs_task_confirmation(task: Mapping[str, Any]) -> bool:
    """任务是否仍待人工确认。

    判定口径与 tools.task_dispatch.dispatch_block_reason 的 「need_confirm」 分支
    完全一致：需要确认（need_confirm 为真）且当前状态还不是 confirmed。
    因此「界面是否提供确认入口」与「后端是否放行下发」不会出现分歧。
    """
    return bool(task.get("need_confirm")) and str(task.get("status") or "") != "confirmed"


def confirm_task(
    task_id: int,
    *,
    operator: str = models.DEFAULT_OPERATOR,
    db_path: Any = None,
) -> dict[str, Any]:
    """人工确认任务：把状态推进为 confirmed，并写入操作日志。

    复用既有 sqlite_db.update_status（自动留痕），不新增任何业务规则、
    不改动任务的其他字段，也不代替「办结标准确认」。
    """
    try:
        sqlite_db.update_status(
            task_id,
            "confirmed",
            operator=operator,
            remark="人工确认任务（下发前置条件）",
            db_path=db_path,
        )
    except ValueError as exc:
        return {"ok": False, "message": f"任务确认失败：{exc}"}
    except sqlite_db.TaskNotFoundError:
        return {"ok": False, "message": f"任务不存在（编号 {task_id}），确认已取消。"}
    except sqlite_db.DatabaseError:
        return {"ok": False, "message": MSG_TASK_CONFIRM_FAILED}
    except Exception:
        return {"ok": False, "message": MSG_TASK_CONFIRM_FAILED}
    return {"ok": True, "message": MSG_TASK_CONFIRMED}


# ============================================================
# 下发前智能分析（v3.0 第一阶段）：任务信息修正 + 下发前检查
# ============================================================

SECTION_EDIT: str = "任务信息修正"
CAPTION_EDIT: str = "下发前核对并补齐关键执行信息；保存会写入操作日志，但不改动任务状态"

SECTION_PREDISPATCH: str = "下发前智能分析"
CAPTION_PREDISPATCH: str = (
    "判断任务是否清楚、能否下发、还缺什么；与是否临期无关，任何待下发任务都可检查"
)
BTN_PREDISPATCH: str = "开始下发前分析"
BTN_SAVE_TASK: str = "保存任务信息"

LBL_TITLE: str = "任务标题"
LBL_DEPARTMENT: str = "责任部门"
LBL_OWNER: str = "负责人"
LBL_DEADLINE_INPUT: str = "截止时间（YYYY-MM-DD）"
LBL_PRIORITY: str = "优先级"
LBL_REQUIREMENT: str = "执行要求"

HINT_PREDISPATCH_IDLE: str = (
    "尚未进行下发前分析。点击上方按钮，由大模型逐项核对任务是否具备下发条件。"
)
HINT_PREDISPATCH_NO_ISSUE: str = "本次分析未发现需要额外处理的问题，可直接预览下发通知。"
HINT_PREDISPATCH_BLOCK: str = (
    "存在关键信息缺失，已阻止直接下发；请先在上方「任务信息修正」中补齐后重新分析。"
)
MSG_PREDISPATCH_RUNNING: str = "正在核对任务是否具备下发条件……"
MSG_PREDISPATCH_FAILED: str = (
    "下发前分析未成功（模型未返回可用结果），这不代表任务无需处理，请稍后重试。"
)
MSG_TASK_SAVED: str = "任务信息已保存，请重新进行下发前分析。"
MSG_TASK_SAVE_FAILED: str = "任务信息保存失败，请稍后重试。"

# 优先级下拉：界面中文 → 内部枚举（含「未设置」，便于清空错误值）
PRIORITY_INPUT_OPTIONS: dict[str, str | None] = {
    "未设置": None,
    "高": "high",
    "中": "medium",
    "低": "low",
}

# 问题清单表格列
COL_PROBLEM: str = "发现问题"
COL_CAUSE: str = "判断原因"
COL_EVIDENCE: str = "参考依据"
COL_SUGGESTION: str = "建议处理"
COL_SEVERITY: str = "严重度"


def save_task_info(
    task_id: int,
    *,
    title: str,
    department: str,
    owner: str,
    deadline: str,
    priority: str | None,
    description: str,
    operator: str = models.DEFAULT_OPERATOR,
    db_path: Any = None,
) -> dict[str, Any]:
    """保存管理者修正后的任务信息（下发前补齐关键字段）。

    只走 sqlite_db.update_task_fields（白名单字段 + 乐观锁 + 留痕），
    不自行拼 SQL，也不改动任务状态。
    """
    try:
        sqlite_db.update_task_fields(
            task_id,
            {
                "title": title,
                "department": department,
                "owner": owner,
                "deadline": deadline,
                "priority": priority,
                "description": description,
            },
            operator=operator,
            remark="人工修正任务信息（下发前核对）",
            db_path=db_path,
        )
    except ValueError as exc:
        return {"ok": False, "message": f"保存失败：{exc}"}
    except sqlite_db.ConcurrentUpdateError:
        return {"ok": False, "message": MSG_DB_CONFLICT}
    except sqlite_db.TaskNotFoundError:
        return {"ok": False, "message": f"任务不存在（编号 {task_id}），保存已取消。"}
    except Exception:
        return {"ok": False, "message": MSG_TASK_SAVE_FAILED}
    return {"ok": True, "message": MSG_TASK_SAVED}


def run_predispatch_analysis(
    task_id: int,
    *,
    llm_fn: Any = None,
    db_path: Any = None,
    today: Any = None,
) -> dict[str, Any]:
    """调用「下发前检查分析」，返回统一结构。

    kind 取值：
        analysis  —— 分析已完成（result.readiness / issues 可用）
        closed    —— 任务已正式闭环，未执行分析（不调用模型）
        error / db_error / unexpected —— **分析未成功**

    「未发现问题」与「分析未成功」是两种完全不同的结果，必须分开展示，
    切勿把它们合并成同一句「无需处理」。
    """
    try:
        result = execution_agent.analyze_before_dispatch(
            task_id, llm_fn=llm_fn, db_path=db_path, today=today
        )
    except execution_agent.ExecutionAnalysisError:
        return {"ok": False, "kind": "error", "message": MSG_PREDISPATCH_FAILED, "result": None}
    except sqlite_db.DatabaseError:
        return {"ok": False, "kind": "db_error", "message": MSG_DB_CONFLICT, "result": None}
    except Exception:
        return {"ok": False, "kind": "unexpected", "message": MSG_PREDISPATCH_FAILED, "result": None}

    return {
        "ok": bool(result.get("ok")),
        "kind": str(result.get("kind") or ""),
        "message": "",
        "result": result,
    }


def predispatch_blocking(task_id: int) -> bool:
    """本任务的下发前分析是否得出「暂不可直接下发」的结论（界面层闸门）。

    仅在**本次会话已分析过、且结论为 blocking** 时返回 True；
    未分析过不会阻断下发（沿用后端的负责人/确认硬门槛）。
    """
    outcome = state.get_predispatch(task_id)
    if not outcome or not outcome.get("ok"):
        return False
    result = outcome.get("result") or {}
    return bool(result.get("blocking"))


# ============================================================
# 页面渲染
# ============================================================


def _render_overview(tasks: Sequence[Mapping[str, Any]]) -> None:
    """任务概览（紧凑版）：一行关键指标 + 一行风险分布。

    只做**展示层计数**（按 status / risk_level / need_confirm 字段实时汇总），
    不派生业务字段、不计算完成率，也不改变既有统计口径。
    压缩目的：不再用多张统计卡占满首屏，把纵向空间让给任务列表与处置工作区。
    """
    def _count(pred: Any) -> int:
        return sum(1 for t in tasks if pred(t))

    risk = styles.RISK_COLORS
    cards.section(OVERVIEW_TITLE, OVERVIEW_CAPTION)
    cards.kpi_row(
        [
            {"label": "任务总数", "value": len(tasks)},
            {"label": "已确认", "value": _count(lambda t: str(t.get("status") or "") == "confirmed")},
            {"label": "已完成", "value": _count(lambda t: str(t.get("status") or "") == "completed")},
            {
                "label": "需人工确认",
                "value": _count(lambda t: bool(t.get("need_confirm"))),
                "desc": OVERVIEW_CONFIRM_HINT,
            },
        ]
    )
    # 风险分布：紧凑一行徽章，取代原先「区块标题 + 圆点统计卡」两段式展示
    cards.badge_row(
        [
            ("低风险", str(_count(lambda t: str(t.get("risk_level") or "") == "green")), risk.get("green", styles.TEXT_SUB)),
            ("中风险", str(_count(lambda t: str(t.get("risk_level") or "") == "yellow")), risk.get("yellow", styles.TEXT_SUB)),
            ("高风险", str(_count(lambda t: str(t.get("risk_level") or "") == "red")), risk.get("red", styles.TEXT_SUB)),
        ]
    )


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
            "请先在「会议材料与任务生成」上传材料，识别并确认后写入库。",
            "前往会议材料与任务生成",
        ):
            state.goto(nav.PAGE_MATERIAL)
        return

    focus_task = _find_task(all_tasks, focus_id)
    if focus_task is not None:
        _render_focus_panel(focus_task)
        st.divider()

    _render_overview(all_tasks)
    st.divider()

    filters = _render_filters(all_tasks)
    filtered = _apply_filters(filters)

    # 视图切换：本次生成的任务（只认会话内 batch_create() 的真实 ID）/ 全部任务
    batch_ids = state.get_last_import_ids()
    view = _render_view_switch(batch_ids)
    if view == VIEW_BATCH:
        filtered = _filter_batch_tasks(filtered, all_tasks, batch_ids)

    cards.section(
        f"{view}（{len(filtered)} / {len(all_tasks)} 条）",
        HINT_TASK_LIST_TITLE,
    )
    if not filtered:
        st.warning("当前筛选条件下没有任务。")
        return

    # 固定高度：51 条任务不再把页面拉成长卷，超出部分由表格自身滚动。
    # 使用 st.dataframe 既有能力，不引入任何新的 UI 框架或第三方组件。
    st.dataframe(tables.tasks_styler(filtered), hide_index=True, use_container_width=True, height=TASK_TABLE_HEIGHT)

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
    """状态 / 部门下推到 list_tasks()，其余条件在展示层过滤。

    排序：本页按「最新创建优先」展示（newest_first=True）。数据库
    list_tasks() 的默认排序仍是入库顺序升序，此处只改变展示顺序，
    不改变任何业务数据，也不改变既有 API 契约。
    """
    tasks = sqlite_db.list_tasks(
        status=filters["status"],
        department=filters["department"],
        newest_first=True,
    )

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


# 默认选中来源标记（用于界面提示"为什么选中了这一条"）
DEFAULT_FOCUS: str = "focus"
DEFAULT_BATCH: str = "batch"
DEFAULT_NEWEST: str = "newest"


def _label_for_id(
    options: Mapping[str, Mapping[str, Any]],
    option_list: Sequence[str],
    task_id: Any,
) -> str | None:
    """按稳定任务 ID 找到对应选项文案；ID 非法或不存在时返回 None。"""
    try:
        wanted = int(task_id)
    except (TypeError, ValueError):
        return None
    for label in option_list:
        try:
            if int(options[label].get("id")) == wanted:
                return label
        except (TypeError, ValueError):
            continue
    return None


def _pick_default_label(
    options: Mapping[str, Mapping[str, Any]],
    option_list: Sequence[str],
    preselect_id: int | None,
    batch_ids: Sequence[int],
) -> tuple[str, str]:
    """决定任务选择器的默认项，返回 (选项文案, 来源标记)。

    优先级（先命中先用）：
        1. focus —— 确认入库 / 首页「查看并处理」指定的**真实任务 ID**；
        2. 本次入库任务 —— 会话内 batch_create() 返回的真实 ID（取第一个）；
        3. 列表第一条 —— 列表已按最新优先排序，即最新任务。
    只用稳定 ID 匹配，不按标题、创建时间或列表位置猜测本次任务。
    """
    if not option_list:
        return "", DEFAULT_NEWEST
    if preselect_id is not None:
        label = _label_for_id(options, option_list, preselect_id)
        if label is not None:
            return label, DEFAULT_FOCUS
    for task_id in batch_ids:
        label = _label_for_id(options, option_list, task_id)
        if label is not None:
            return label, DEFAULT_BATCH
    return option_list[0], DEFAULT_NEWEST


def _task_id_in(task: Mapping[str, Any], wanted: set[int]) -> bool:
    """任务 ID 是否在给定集合内（脏数据一律视为不在）。"""
    try:
        return int(task.get("id")) in wanted
    except (TypeError, ValueError):
        return False


def _render_view_switch(batch_ids: Sequence[int]) -> str:
    """任务视图切换：本次生成的任务 / 全部任务。

    没有可靠的「本次任务 ID 集合」时只提供「全部任务」并明确说明原因；
    不按创建时间、标题或 ID 大小推测本次任务。
    """
    options = [VIEW_BATCH, VIEW_ALL] if batch_ids else [VIEW_ALL]
    if st.session_state.get(KEY_TASK_VIEW) not in options:
        st.session_state[KEY_TASK_VIEW] = VIEW_BATCH if batch_ids else VIEW_ALL
    view = str(st.radio(LBL_TASK_VIEW, options, horizontal=True, key=KEY_TASK_VIEW))

    if not batch_ids:
        st.caption(CAPTION_VIEW_NO_BATCH)
        return view

    if view == VIEW_BATCH:
        created_at = state.get_last_import_at()
        ids_text = "、".join(f"#{int(x)}" for x in batch_ids)
        st.caption(
            f"本次会话已确认入库 {len(batch_ids)} 项任务（{ids_text}）"
            + (f"，入库时间：{created_at}" if created_at else "")
            + "。" + CAPTION_VIEW
        )
    return view


def _filter_batch_tasks(
    filtered: Sequence[Mapping[str, Any]],
    all_tasks: Sequence[Mapping[str, Any]],
    batch_ids: Sequence[int],
) -> list[dict[str, Any]]:
    """只保留本批真实任务 ID 对应的任务。

    结果为空时如实区分「被筛选条件排除」与「任务已不存在」，
    绝不退回展示历史任务冒充本次任务。
    """
    wanted = {int(x) for x in batch_ids}
    if not wanted:
        return []
    kept = [dict(task) for task in filtered if _task_id_in(task, wanted)]
    if kept:
        return kept
    if any(_task_id_in(task, wanted) for task in all_tasks):
        st.info(NOTE_BATCH_FILTERED_OUT)
    else:
        st.warning(NOTE_BATCH_MISSING)
    return []


def _render_focus_panel(task: Mapping[str, Any]) -> None:
    """当前处置任务：从首页「查看并处理」跳转过来时的第一视觉焦点。

    v2.2 起这里就是一个完整的「任务处置工作区」：
        任务详情 → 任务确认 → 系统内下发 → 智能处置分析
    与列表模式保持同一顺序，避免出现「两处各有一套互不衔接的区块」。
    只做展示与既有能力复用，不新增业务判断，也不调用大模型。
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
    _render_task_editor(task, task_id, scope="focus")

    st.write("")
    _render_task_confirmation(task, task_id, scope="focus")

    st.write("")
    _render_predispatch_panel(task, task_id, scope="focus")

    st.write("")
    # 第三页只负责「下发」：直接渲染下发面板（通知预览 + 系统内模拟下发 + 发送记录）。
    # 执行回执 / 执行反馈 / 执行期督办已全部迁往第四页「执行反馈与检查」。
    submission_view.render_dispatch_panel(
        task, task_id, scope="focus", block_dispatch=predispatch_blocking(task_id)
    )


def _render_actions(
    tasks: Sequence[Mapping[str, Any]],
    preselect_id: int | None = None,
) -> None:
    """任务操作区（列表模式）。

    两种模式：
    - **专注模式**（首页「查看并处理」跳转，preselect_id 非空）：
      目标任务已在页面顶部「当前处置任务」区完整呈现（工作区全流程），
      此处**只保留任务切换入口**，不再重复渲染同一任务的详情与智能处置。
      用户切换到其他任务后，才恢复完整的操作区。
    - **普通管理模式**（直接从导航进入，preselect_id 为 None）：
      完整渲染「任务处置工作区 + 折叠的管理操作」，能力与改造前一致。

    工作区顺序固定：任务详情 → 任务确认 → 系统内下发 → 智能处置分析。
    任务确认在这里**直接可见**，不放入折叠区：它是正式下发的前置条件，
    不是低频管理操作。
    """
    options = {tables.task_option_label(task): task for task in tasks}
    option_list = list(options)

    # 默认选中规则见 _pick_default_label：focus > 本次入库任务 > 列表第一条。
    # 列表已按最新优先排序，因此第三条兜底也落在最新任务上，不会悄悄跳回旧任务。
    default_label, default_reason = _pick_default_label(
        options, option_list, preselect_id, state.get_last_import_ids()
    )
    # 稳定 widget key + 安全重置：新增任务或切换筛选后，旧选中值可能已不在选项里；
    # 此时按默认规则重置，不保留指向旧任务的陈旧选择，也不清空整个 session_state。
    if st.session_state.get(KEY_TASK_SELECT) not in options:
        st.session_state[KEY_TASK_SELECT] = default_label

    # 专注模式：先给「切换任务」区块标题，再放选择器。
    # Streamlit 自上而下渲染，若把标题放在选择器之后，会出现
    # 「选择器悬在区块标题之上」的错位观感（标题看起来属于下一块）。
    if preselect_id is not None:
        cards.section(
            "切换任务",
            "当前处置任务已在页面顶部展示；选择其他任务即可进行状态修改与智能处置",
        )

    if preselect_id is not None and default_reason != DEFAULT_FOCUS:
        st.info(NOTE_FOCUS_NOT_VISIBLE)
    elif default_reason == DEFAULT_BATCH:
        st.caption(NOTE_BATCH_SELECTED)

    selected_label = st.selectbox("选择任务", option_list, key=KEY_TASK_SELECT)
    task = options[selected_label]
    task_id = int(task["id"])

    # 仍停在 focus 任务：顶部「当前处置任务」已完整呈现，不再重复渲染
    if preselect_id is not None and task_id == int(preselect_id):
        return

    # ---------- 任务处置工作区（详情 → 确认 → 下发 → 智能处置）----------
    cards.section(WORKSPACE_TITLE, WORKSPACE_CAPTION)

    _render_detail(task)

    st.write("")
    _render_task_editor(task, task_id, scope="list")

    st.write("")
    _render_task_confirmation(task, task_id, scope="list")

    st.write("")
    _render_predispatch_panel(task, task_id, scope="list")

    st.write("")
    # 同专注模式：第三页只保留下发面板，不再承载执行期反馈与督办。
    submission_view.render_dispatch_panel(
        task, task_id, scope="list", block_dispatch=predispatch_blocking(task_id)
    )

    # 低频管理操作（状态编辑、操作日志）折叠，避免干扰演示主流程。
    # 注意：任务确认已被提到工作区，这里只保留完整的状态编辑与日志。
    st.write("")
    with st.expander("管理操作（修改状态 / 查看操作日志）", expanded=False):
        cards.section("任务状态修改", "状态变更会自动写入操作日志")
        _render_status_form(task, task_id)

        st.write("")
        cards.section("操作日志", "状态变更、下发与智能处置全程留痕")
        logs = sqlite_db.get_logs(task_id)
        if logs:
            st.dataframe(tables.logs_dataframe(logs), hide_index=True, use_container_width=True)
        else:
            st.caption("该任务暂无操作日志。")


def _render_task_confirmation(task: Mapping[str, Any], task_id: int, scope: str = "list") -> None:
    """任务确认：正式下发的前置条件，始终直接可见。

    - 待确认：说明「未确认不能下发」，并给出清晰可见的确认入口；
    - 已满足：只展示真实确认状态，不再重复摆放无意义的确认表单。

    刻意与「办结标准已确认」分开陈述：后者是闭环依据，不等于任务已确认。
    """
    cards.section(CONFIRM_SECTION, CONFIRM_CAPTION)

    if needs_task_confirmation(task):
        st.warning(f"{HINT_TASK_PENDING_CONFIRM}：{HINT_CONFIRM_BLOCKS_DISPATCH}")
        operator = st.text_input(
            LBL_CONFIRM_OPERATOR,
            value=models.DEFAULT_OPERATOR,
            key=f"cf_confirm_op_{scope}_{task_id}",
        )
        if st.button(
            BTN_CONFIRM_TASK,
            key=f"cf_confirm_task_{scope}_{task_id}",
            type="primary",
        ):
            outcome = confirm_task(
                task_id, operator=operator.strip() or models.DEFAULT_OPERATOR
            )
            if outcome["ok"]:
                state.set_flash(outcome["message"])
                st.rerun()
            else:
                st.error(outcome["message"])
        st.caption(HINT_CONFIRM_NOT_CRITERIA)
        return

    st.success(
        f"{HINT_TASK_CONFIRM_SATISFIED}（当前状态：{tables.status_label(task.get('status'))}）。"
    )
    st.caption(HINT_CONFIRM_NO_FORM)


def _render_task_editor(task: Mapping[str, Any], task_id: int, scope: str = "list") -> None:
    """任务信息修正区：下发前补齐/更正关键执行信息。

    为什么必须在第三页提供：识别阶段可能没有负责人或截止时间，
    而这两项缺失时下发会被硬门槛挡住；若此处不能补齐，任务将永久无法下发。
    """
    cards.section(SECTION_EDIT, CAPTION_EDIT)

    current_priority = str(task.get("priority") or "")
    priority_labels = list(PRIORITY_INPUT_OPTIONS)
    default_index = next(
        (
            index
            for index, label in enumerate(priority_labels)
            if PRIORITY_INPUT_OPTIONS[label] == current_priority
        ),
        0,
    )

    with st.form(key=f"cf_edit_form_{scope}_{task_id}"):
        title = st.text_input(LBL_TITLE, value=str(task.get("title") or ""))
        row1_left, row1_right = st.columns(2)
        department = row1_left.text_input(LBL_DEPARTMENT, value=str(task.get("department") or ""))
        owner = row1_right.text_input(LBL_OWNER, value=str(task.get("owner") or ""))
        row2_left, row2_right = st.columns(2)
        deadline = row2_left.text_input(LBL_DEADLINE_INPUT, value=str(task.get("deadline") or ""))
        priority_label = row2_right.selectbox(LBL_PRIORITY, priority_labels, index=default_index)
        description = st.text_area(
            LBL_REQUIREMENT, value=str(task.get("description") or ""), height=80
        )
        submitted = st.form_submit_button(BTN_SAVE_TASK)

    if not submitted:
        st.caption("「负责人 / 截止时间 / 执行要求」缺失时无法直接下发，请在此补齐。")
        return

    outcome = save_task_info(
        task_id,
        title=title,
        department=department,
        owner=owner,
        deadline=deadline,
        priority=PRIORITY_INPUT_OPTIONS[priority_label],
        description=description,
    )
    if outcome["ok"]:
        # 信息已变，旧的分析结论不再适用，必须作废
        state.clear_predispatch()
        state.set_flash(outcome["message"])
        st.rerun()
    else:
        st.error(outcome["message"])


def _render_predispatch_panel(task: Mapping[str, Any], task_id: int, scope: str = "list") -> None:
    """下发前智能分析区：发起分析 → 展示结论与问题清单。"""
    cards.section(SECTION_PREDISPATCH, CAPTION_PREDISPATCH)

    if st.button(
        BTN_PREDISPATCH,
        key=f"cf_predispatch_run_{scope}_{task_id}",
        type="primary",
    ):
        with st.spinner(MSG_PREDISPATCH_RUNNING):
            outcome = run_predispatch_analysis(task_id)
        state.save_predispatch(task_id, outcome)
        st.rerun()

    outcome = state.get_predispatch(task_id)
    if not outcome:
        st.caption(HINT_PREDISPATCH_IDLE)
        return

    _render_predispatch_result(outcome)


def _render_predispatch_result(outcome: Mapping[str, Any]) -> None:
    """展示下发前分析结果。

    三类结果严格区分：
        分析未成功（error/db_error/unexpected）→ 红色错误，绝不显示「无需处理」；
        已闭环（closed）                        → 中性提示；
        分析完成（analysis）                    → 结论 + 问题清单；无问题时明确说明。
    """
    kind = str(outcome.get("kind") or "")
    if kind in ("error", "db_error", "unexpected"):
        st.error(str(outcome.get("message") or MSG_PREDISPATCH_FAILED))
        return

    result = outcome.get("result") or {}
    if kind == "closed":
        st.info(str(result.get("summary") or "该任务已正式闭环，无需再做下发前分析。"))
        return

    readiness = str(result.get("readiness") or "")
    st.markdown(f"**整体结论：{labels.readiness_label(readiness)}**")
    st.write(str(result.get("summary") or ""))

    issues = [item for item in (result.get("issues") or []) if isinstance(item, Mapping)]
    if not issues:
        st.success(HINT_PREDISPATCH_NO_ISSUE)
    else:
        st.dataframe(
            [
                {
                    COL_PROBLEM: str(item.get("problem") or ""),
                    COL_CAUSE: str(item.get("cause") or ""),
                    COL_EVIDENCE: str(item.get("evidence") or ""),
                    COL_SUGGESTION: str(item.get("suggestion") or ""),
                    COL_SEVERITY: labels.severity_label(item.get("severity")),
                }
                for item in issues
            ],
            hide_index=True,
            use_container_width=True,
        )

    missing = [str(field) for field in (result.get("missing_fields") or [])]
    if missing:
        st.warning("关键信息缺失：" + "、".join(labels.field_label(f) for f in missing) + "。")
    if result.get("blocking"):
        st.warning(HINT_PREDISPATCH_BLOCK)


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
