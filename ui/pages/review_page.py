"""识别结果编辑与确认入库（v3.0 起内联在「会议材料与任务生成」页内）。

本模块不再作为独立主页面（原「事项智能识别」已并入会议材料页），对外提供：
    render_inline(tasks)  在会议材料页内就地渲染「识别结果 → 修正 → 确认入库」
    render()              兼容入口：保留页头与空状态，当前导航不再引用

两个入口共用同一套展示与入库逻辑，避免两处实现漂移。
展示 PlannerAgent 的输出（中文徽章 + 可编辑表格），人工修正后点击「确认入库」，
调用 database.sqlite_db.batch_create() 写入任务数据库。

说明：入库前会再用 planner_agent.plan() 复核一遍（负责人/优先级被人工补齐后，
风险等级与人工确认标记会随之刷新），本页面不自行计算任何业务字段。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Mapping, Sequence

import pandas as pd
import streamlit as st

from agent import planner_agent, reminder_agent
from config import settings
from database import sqlite_db
from ui.components import cards, state, styles, tables
from ui.pages import nav

# 页面标题与副标题
PAGE_TITLE: str = "事项智能识别"
PAGE_SUBTITLE: str = "人工确认后写入任务数据库"

# 可编辑列
COL_TITLE = "标题"
COL_OWNER = "负责人"
COL_DEPARTMENT = "部门"
COL_DEADLINE = "截止时间"
COL_PRIORITY = "优先级"
COL_DESCRIPTION = "事项说明"

# 只读列（由 PlannerAgent 产出，人工不直接修改）
COL_RISK = "风险等级"
COL_CONFIRM = "需人工确认"

# 优先级下拉选项：显示中文 → 存储枚举
PRIORITY_OPTIONS: dict[str, str] = {"高": "high", "中": "medium", "低": "low"}

# 表格列顺序
EDITOR_COLUMNS: tuple[str, ...] = (
    COL_TITLE,
    COL_OWNER,
    COL_DEPARTMENT,
    COL_DEADLINE,
    COL_PRIORITY,
    COL_DESCRIPTION,
    COL_RISK,
    COL_CONFIRM,
)


def render() -> None:
    """兼容入口：保留页头与空状态引导（当前导航已不再引用本页）。"""
    cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)

    flash = state.pop_flash()
    if flash:
        st.success(flash)

    tasks = state.get_parsed_tasks()
    if not tasks:
        if cards.empty_state("暂无待确认事项", "请先上传会议纪要或任务台账。", "前往会议材料与任务生成"):
            state.goto(nav.PAGE_MATERIAL)
        return

    render_inline(tasks)


def render_inline(tasks: Sequence[Mapping[str, Any]]) -> None:
    """在会议材料页内就地渲染「识别结果 → 修正 → 确认入库」。

    调用方负责页头与空状态；这里只渲染「已有识别结果」时的内容，
    保证没有材料时界面上不会出现看似属于本次材料的识别结果。
    """
    left, right = st.columns([10, 12], gap="large")
    with left:
        _render_overview(tasks)
    with right:
        _render_risk_panel(tasks)

    st.write("")
    cards.section("识别结果（可编辑）", "修改后点击「确认入库」写入任务数据库")
    edited = st.data_editor(
        _build_editor_frame(tasks),
        column_config=_column_config(),
        disabled=[COL_RISK, COL_CONFIRM],
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key="cf_review_editor",
    )

    with st.expander("查看识别结果原始字段", expanded=False):
        st.code(json.dumps(tasks, ensure_ascii=False, indent=2), language="json")

    st.write("")
    confirm_col, clear_col = st.columns([2, 1])
    with confirm_col:
        confirm_clicked = st.button("确认入库", type="primary", use_container_width=True)
    with clear_col:
        if st.button("清空本次识别结果", use_container_width=True):
            state.clear_parsed()
            st.rerun()

    if confirm_clicked:
        _import_tasks(edited)


# ------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------


def _render_overview(tasks: Sequence[Mapping[str, Any]]) -> None:
    """左栏：概览统计（仅展示数量）。"""
    cards.section("概览统计")
    cards.level_stats(
        [
            ("待确认", len(tasks), styles.PRIMARY),
            ("高风险", _count_high_risk(tasks), styles.DANGER),
            ("缺负责人", _count_missing_owner(tasks), styles.WARNING),
        ]
    )


def _render_risk_panel(tasks: Sequence[Mapping[str, Any]]) -> None:
    """右栏：风险事项列表（圆点统计 + 每项中文徽章）。"""
    cards.section("风险事项列表")
    cards.dot_list(
        [
            ("高风险", _count_high_risk(tasks), styles.DANGER),
            ("需确认", _count_need_confirm(tasks), styles.WARNING),
            ("缺负责人", _count_missing_owner(tasks), styles.WARNING),
            ("今日截止", _count_due_today(tasks), styles.URGENT),
        ]
    )

    st.write("")
    cards.badge_row(
        [
            (
                f"#{index + 1} {task.get('title') or '未命名事项'}",
                tables.risk_label(task.get("risk_level")),
                styles.RISK_COLORS.get(str(task.get("risk_level") or ""), styles.TEXT_MUTED),
            )
            for index, task in enumerate(tasks)
        ]
    )


def _count_high_risk(tasks: Sequence[Mapping[str, Any]]) -> int:
    """高风险事项数量。"""
    return sum(1 for task in tasks if task.get("risk_level") == "red")


def _count_need_confirm(tasks: Sequence[Mapping[str, Any]]) -> int:
    """需确认事项数量。"""
    return sum(1 for task in tasks if task.get("risk_level") == "yellow")


def _count_missing_owner(tasks: Sequence[Mapping[str, Any]]) -> int:
    """缺少负责人的事项数量。"""
    return sum(1 for task in tasks if not task.get("owner"))


def _count_due_today(tasks: Sequence[Mapping[str, Any]]) -> int:
    """今日截止事项数量：复用提醒模块的剩余天数计算，页面不重算规则。"""
    today = date.today()
    return sum(
        1 for task in tasks if reminder_agent.compute_days_left(task.get("deadline"), today) == 0
    )


def _build_editor_frame(tasks: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """把 PlannerAgent 输出转换为可编辑表格（风险等级显示中文）。"""
    deadlines = pd.to_datetime([task.get("deadline") for task in tasks], errors="coerce")

    return pd.DataFrame(
        [
            {
                COL_TITLE: task.get("title") or "",
                COL_OWNER: task.get("owner") or "",
                COL_DEPARTMENT: task.get("department") or "",
                COL_DEADLINE: deadlines[index].date() if not pd.isna(deadlines[index]) else None,
                COL_PRIORITY: _priority_label(task.get("priority")),
                COL_DESCRIPTION: task.get("description") or "",
                COL_RISK: tables.risk_label(task.get("risk_level")),
                COL_CONFIRM: "是" if task.get("need_confirm") else "否",
            }
            for index, task in enumerate(tasks)
        ],
        columns=list(EDITOR_COLUMNS),
    )


def _priority_label(priority: Any) -> str:
    """优先级枚举 → 中文标签（缺失时留空，由人工选择）。"""
    text = str(priority or "").strip()
    return {value: label for label, value in PRIORITY_OPTIONS.items()}.get(text, "")


def _column_config() -> dict[str, Any]:
    """表格列配置：标题必填、优先级下拉、日期选择器。"""
    return {
        COL_TITLE: st.column_config.TextColumn(COL_TITLE, required=True, width="medium"),
        COL_OWNER: st.column_config.TextColumn(COL_OWNER, help="缺失时请人工补充"),
        COL_DEPARTMENT: st.column_config.TextColumn(COL_DEPARTMENT, help="责任部门"),
        COL_DEADLINE: st.column_config.DateColumn(COL_DEADLINE, format="YYYY-MM-DD", help="缺失会被标记为高风险"),
        COL_PRIORITY: st.column_config.SelectboxColumn(COL_PRIORITY, options=list(PRIORITY_OPTIONS), help="高 / 中 / 低"),
        COL_DESCRIPTION: st.column_config.TextColumn(COL_DESCRIPTION, width="large"),
        COL_RISK: st.column_config.TextColumn(COL_RISK, help="入库前自动复核"),
        COL_CONFIRM: st.column_config.TextColumn(COL_CONFIRM, help="入库前自动复核"),
    }


def _confirm_import(
    edited: pd.DataFrame, *, last_fingerprint: str | None
) -> dict[str, Any]:
    """把人工确认后的表格写入数据库（纯编排，不依赖 Streamlit 运行时，便于单测）。

    会话级幂等：``last_fingerprint`` 为本会话已入库批次的内容指纹；若与本次
    待确认内容一致，直接拒绝——**同一会话内同一批结果只允许入库一次**，
    杜绝双击 / 重复点击 / 重新载入同一材料造成的整批重复。拒绝时不写任何
    数据；内容不同则正常走「复核 → 入库」，并回传本次指纹供调用方记录。

    返回：``{ok, reason, fingerprint, created_ids}``
    """
    tasks = _rows_to_tasks(edited)
    if not tasks:
        return {
            "ok": False,
            "reason": "没有可入库的任务（标题为空的行会被忽略）。",
            "fingerprint": "",
            "created_ids": [],
        }

    fingerprint = state.compute_import_fingerprint(tasks)
    if last_fingerprint and fingerprint and last_fingerprint == fingerprint:
        return {
            "ok": False,
            "reason": (
                "该批识别结果已入库，请勿重复确认。"
                "如需重新导入，请修改内容或刷新页面后重试。"
            ),
            "fingerprint": fingerprint,
            "created_ids": [],
        }

    try:
        # 人工补齐字段后重新规划，刷新风险等级与人工确认标记
        planned = planner_agent.plan(tasks)
        created = sqlite_db.batch_create(planned)
    except (ValueError, TypeError) as exc:
        return {
            "ok": False,
            "reason": f"入库失败：{exc}",
            "fingerprint": fingerprint,
            "created_ids": [],
        }

    created_ids = _created_ids(created)
    if not created_ids:
        return {
            "ok": False,
            "reason": "入库未返回有效的任务记录，本次不会更新任务定位，请核对任务列表。",
            "fingerprint": fingerprint,
            "created_ids": [],
        }
    return {
        "ok": True,
        "reason": "",
        "fingerprint": fingerprint,
        "created_ids": created_ids,
    }


def _import_tasks(edited: pd.DataFrame) -> None:
    """确认入库的 UI 编排：幂等守卫 → 入库 → 会话记录 → 跳转任务督办中心。

    守卫只拦「同一会话内同一批待确认内容」的重复确认；不同内容（含人工
    修正）指纹不同，正常放行——新增一条不同的真实任务，总数加一。
    """
    outcome = _confirm_import(edited, last_fingerprint=state.get_last_import_fingerprint())
    if not outcome["ok"]:
        st.error(outcome["reason"])
        return

    created_ids = outcome["created_ids"]
    state.clear_parsed()
    state.set_last_import(created_ids, created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    state.set_last_import_fingerprint(outcome["fingerprint"])
    state.set_focus_task(created_ids[0])
    state.set_flash(
        f"已成功入库 {len(created_ids)} 项任务，已定位到本次生成的任务。"
    )
    state.goto(nav.PAGE_TASKS)


def _created_ids(created: Sequence[Mapping[str, Any]]) -> list[int]:
    """从 batch_create() 的返回记录中取出真实任务 ID（保序、去重、跳过脏数据）。"""
    ids: list[int] = []
    for record in created or []:
        try:
            number = int(record.get("id"))
        except (AttributeError, TypeError, ValueError):
            continue
        if number not in ids:
            ids.append(number)
    return ids


def _rows_to_tasks(edited: pd.DataFrame) -> list[dict[str, Any]]:
    """把编辑后的表格还原为任务字典（与 PlannerAgent 输出结构一致）。"""
    tasks: list[dict[str, Any]] = []
    for _, row in edited.iterrows():
        title = _text(row.get(COL_TITLE))
        if not title:  # 跳过空行（表格允许新增行）
            continue
        tasks.append(
            {
                "title": title,
                "owner": _text(row.get(COL_OWNER)),
                "department": _text(row.get(COL_DEPARTMENT)),
                "deadline": _deadline(row.get(COL_DEADLINE)),
                "priority": PRIORITY_OPTIONS.get(_text(row.get(COL_PRIORITY)) or ""),
                "status": settings.DEFAULT_STATUS,
                "description": _text(row.get(COL_DESCRIPTION)),
            }
        )
    return tasks


def _text(value: Any) -> str | None:
    """单元格文本清洗：空值/NaN 统一为 None。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _deadline(value: Any) -> str | None:
    """截止日期单元格 → YYYY-MM-DD 字符串。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip() or None
