"""页面五：执行情况汇报。

自上而下：领导摘要 → 四张摘要卡 → 统计视图 → 完整周报正文。
数据全部来自已有业务接口，页面不做任何统计计算：
- report_agent.generate_dashboard()      驾驶舱数据（摘要卡与统计视图）
- report_agent.build_stats()             明细统计
- report_agent.build_suggestions()       规则化执行建议
- report_agent.generate_weekly_report()  周报正文
- reminder_agent.check_all()             重点事项（仅用于组织摘要文案）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

import pandas as pd
import streamlit as st

from agent import reminder_agent, report_agent
from database import sqlite_db
from tools import task_dispatch
from ui import labels
from ui.components import cards, styles

# 页面标题与副标题
PAGE_TITLE: str = "执行情况汇报"
PAGE_SUBTITLE: str = "一键生成高校基层事务周报"

# 标题右侧状态标签
PAGE_TAG: str = "AI 自动生成 · 可直接汇报"

# 导出文件名
REPORT_FILENAME: str = "CampusFlow_周报.md"

# 报告滚动容器高度
REPORT_VIEW_HEIGHT: int = 560

# 图表格高度
CHART_HEIGHT: int = 260

# 需要优先督办的事项级别
FOCUS_LEVELS: tuple[str, ...] = ("overdue", "urgent")

# 「重新生成」后的提示文案（存 session_state 以跨 rerun 展示）
NOTE_KEY: str = "cf_report_note"
NOTE_TEXT: str = "报告已根据最新数据重新生成。"


def render() -> None:
    """渲染执行情况汇报页面。"""
    cards.page_header(PAGE_TITLE, PAGE_SUBTITLE, tag=PAGE_TAG)

    dashboard = report_agent.generate_dashboard()
    stats = report_agent.build_stats()
    suggestions = report_agent.build_suggestions(stats)
    reminders = reminder_agent.check_all()

    cards.leader_summary(_leader_lines(dashboard, reminders))
    _render_summary_cards(dashboard, stats, suggestions)

    st.write("")
    left, right = st.columns([1, 1], gap="large")
    with left:
        _render_level_stats(dashboard)
    with right:
        _render_top_departments(dashboard)

    st.write("")
    _render_submission_check()

    st.write("")
    _render_report()
    cards.note("本报告基于演示数据自动生成，仅用于比赛展示。")


# ------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------


def _render_submission_check() -> None:
    """执行回执检查区块。

    全部数字来自 SQLite 的确定性统计（tools.task_dispatch.summary），
    **不调用大模型**：提交率是算出来的，不是模型说的。
    """
    cards.section("执行回执检查", "基于任务下发记录与负责人回执的真实数据统计")

    try:
        data = task_dispatch.summary()
    except sqlite_db.DatabaseError:
        st.error("执行回执数据读取失败，请稍后重试。")
        return

    if not data["total"]:
        cards.empty_state("暂无任务数据", "请先在「事项智能识别」中确认任务后再查看执行检查。")
        return

    cards.kpi_row(
        [
            {"label": "任务总数", "value": str(data["total"]), "color": styles.PRIMARY},
            {"label": "已下发", "value": str(data["dispatched"]), "color": styles.PRIMARY},
            {
                "label": labels.SUBMISSION_LABELS["submitted"],
                "value": str(data["submitted"]),
                "color": styles.SUCCESS,
            },
            {
                "label": labels.SUBMISSION_LABELS["pending_submit"],
                "value": str(data["pending"]),
                "color": styles.WARNING,
            },
            {
                "label": labels.SUBMISSION_LABELS["overdue_unsubmitted"],
                "value": str(data["overdue"]),
                "color": styles.DANGER,
            },
            {"label": "提交率", "value": f"{data['rate']}%", "color": styles.PRIMARY},
        ]
    )

    st.write("")
    rows = [
        [
            str(row["title"]) or labels.UNKNOWN_TEXT,
            str(row["department"]) or labels.UNKNOWN_TEXT,
            str(row["owner"]) or labels.UNKNOWN_TEXT,
            str(row["deadline"]) or labels.UNKNOWN_TEXT,
            labels.submission_label(row["state"]),
            str(row["submitted_at"]) or labels.UNKNOWN_TEXT,
        ]
        for row in data["rows"]
    ]
    st.dataframe(
        pd.DataFrame(
            rows,
            columns=["任务名称", "部门", "负责人", "截止时间", "回执状态", "提交时间"],
        ),
        hide_index=True,
        use_container_width=True,
    )


def _leader_lines(dashboard: Mapping[str, Any], reminders: Sequence[Mapping[str, Any]]) -> list[str]:
    """领导摘要文案：先给结论数字，再给处置建议（全部取自既有接口输出）。"""
    first = (
        f"本周共执行 {dashboard['total']} 项任务，完成率 {dashboard['completion_rate']}%，"
        f"其中{labels.level_label('overdue')} {dashboard['overdue']} 项、"
        f"{labels.level_label('urgent')} {dashboard['urgent']} 项。"
    )

    focus = [item for item in reminders if item.get("level") in FOCUS_LEVELS]
    due_soon_department = next(
        (
            item.get("department")
            for item in reminders
            if item.get("level") == "due_soon" and item.get("department")
        ),
        None,
    )

    clauses: list[str] = []
    if focus:
        top = focus[0]
        clauses.append(f"建议优先督办{top.get('department') or '相关'}的「{top.get('title') or '重点事项'}」")
    if due_soon_department:
        prefix = "并" if clauses else "建议"
        clauses.append(
            f"{prefix}协调{due_soon_department}完成{labels.level_label('due_soon')}事项"
        )
    if not clauses:
        clauses.append("各项任务按计划有序推进")

    return [first, "，".join(clauses) + "。"]


def _render_summary_cards(
    dashboard: Mapping[str, Any],
    stats: Mapping[str, Any],
    suggestions: Sequence[str],
) -> None:
    """四张摘要卡片：总任务 / 完成率 / 风险事项 / 执行建议。"""
    departments = stats.get("departments") or []
    headline = _short_suggestion(suggestions[0]) if suggestions else "暂无建议"

    cards.kpi_row(
        [
            {
                "label": "总任务",
                "value": dashboard["total"],
                "desc": f"覆盖 {len(departments)} 个责任部门",
            },
            {
                "label": "完成率",
                "value": f"{dashboard['completion_rate']}%",
                "desc": f"已完成 {dashboard['completed']} 项",
            },
            {
                "label": "风险事项",
                "value": f"{dashboard['overdue']} 项{labels.level_label('overdue')}",
                "desc": f"另有 {dashboard['urgent']} 项{labels.level_label('urgent')}待跟进",
                "color": styles.DANGER,
            },
            {
                "label": "执行建议",
                "value": headline,
                "desc": "基于数据的规则化分析",
                "text": True,
            },
        ]
    )


def _short_suggestion(text: str) -> str:
    """建议文案精简：去掉「建议」前缀与句末句号，便于放入卡片。"""
    return str(text).removeprefix("建议").rstrip("。")


def _render_level_stats(dashboard: Mapping[str, Any]) -> None:
    """四级提醒统计。"""
    cards.section("四级提醒统计", "按剩余时间分级")

    rows = [
        (
            reminder_agent.level_label(level),
            int(dashboard[level]),
            styles.LEVEL_COLORS.get(level, styles.PRIMARY),
        )
        for level in reminder_agent.REMINDER_LEVELS
    ]
    cards.level_stats(rows)


def _render_top_departments(dashboard: Mapping[str, Any]) -> None:
    """Top3 部门任务量柱状图。"""
    cards.section("Top3 部门", "按任务数量排序")

    top_departments = dashboard["top_departments"]
    if not top_departments:
        st.caption("暂无部门数据。")
        return

    frame = pd.DataFrame(list(top_departments)).rename(columns={"department": "部门", "count": "任务数"})
    st.bar_chart(frame.set_index("部门"), color=styles.PRIMARY, height=CHART_HEIGHT)


def _render_report() -> None:
    """本周执行报告正文与操作按钮。"""
    cards.section("本周执行报告")

    generate_col, download_col = st.columns([1, 1])
    with generate_col:
        if st.button("重新生成", use_container_width=True):
            st.session_state[NOTE_KEY] = NOTE_TEXT
            st.rerun()

    report = report_agent.generate_weekly_report(generated_at=datetime.now())

    with download_col:
        st.download_button(
            "导出执行报告",
            data=report.encode("utf-8"),
            file_name=REPORT_FILENAME,
            mime="text/markdown",
            use_container_width=True,
        )

    note = st.session_state.pop(NOTE_KEY, None)
    if note:
        st.success(note)

    with st.container(height=REPORT_VIEW_HEIGHT):
        st.markdown(report)
