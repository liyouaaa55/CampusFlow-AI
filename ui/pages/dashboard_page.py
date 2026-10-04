"""页面一：智能驾驶舱。

数据全部来自 ReportAgent，页面不做任何统计计算：
- report_agent.generate_dashboard()  总任务 / 完成率 / 临期 / 紧急 / 逾期 / Top3 部门
- report_agent.build_stats()         明细与部门统计（供建议生成使用）
- report_agent.build_suggestions()   规则化督办建议
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import pandas as pd
import streamlit as st

from agent import reminder_agent, report_agent
from llm import client
from ui.components import cards, pipeline, state, styles

# 页面标题与副标题
PAGE_TITLE: str = "智能驾驶舱"
PAGE_SUBTITLE: str = "今日基层事务执行概况"

# 督办建议最多展示条数（与 ReportAgent 的建议上限一致）
MAX_SUGGESTIONS_SHOWN: int = report_agent.MAX_SUGGESTIONS

# 柱状图高度
CHART_HEIGHT: int = 300

# 横幅关注的提醒级别（顺序即优先级）
FOCUS_LEVELS: tuple[str, ...] = ("overdue", "urgent")


def render() -> None:
    """渲染智能驾驶舱。"""
    dashboard = report_agent.generate_dashboard()
    stats = report_agent.build_stats()
    suggestions = report_agent.build_suggestions(stats)

    _render_header()
    _render_kpi(dashboard)
    _render_focus_banner()

    st.write("")
    left, right = st.columns([11, 9], gap="large")
    with left:
        _render_department_ranking(dashboard)
    with right:
        _render_suggestions(suggestions)

    cards.note("数据基于实时任务数据库统计，切换页面自动刷新。")


# ------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------


def _render_header() -> None:
    """标题区：左侧页面标题，右上角「一键体验演示」。"""
    title_col, action_col = st.columns([5, 2], gap="small")

    with title_col:
        cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)

    with action_col:
        st.write("")
        if st.button("一键体验演示", type="primary", use_container_width=True):
            _run_demo()


def _run_demo() -> None:
    """一键体验演示：载入内置会议纪要 → 智能解析 → 跳转事项识别。

    仅编排已有流程（pipeline.identify_and_plan 内部调用 TaskAgent 与 PlannerAgent），
    不新增任何业务规则，也不直接写库。
    """
    text = pipeline.load_demo_document()
    if not text:
        st.error("未找到内置演示文档，请检查知识库模板文件是否完整。")
        return

    planned: list[dict[str, Any]] = []
    with st.status("正在执行一键体验演示…", expanded=True) as status:
        st.write("① 载入内置会议纪要模板")
        st.write("② 文本解析")
        try:
            planned = pipeline.identify_and_plan(text, source_type=pipeline.DEMO_SOURCE_TYPE)
        except client.LLMError as exc:
            status.update(label="演示未完成", state="error")
            st.error(f"调用大模型失败：{exc}")
            return
        except ValueError as exc:
            status.update(label="演示未完成", state="error")
            st.error(f"解析失败：{exc}")
            return
        st.write(f"③ 智能事项识别：识别到 {len(planned)} 项事项")
        status.update(label=f"识别完成，共 {len(planned)} 项事项", state="complete")

    state.save_parsed(planned, pipeline.DEMO_FILE_LABEL, text)
    state.set_flash(f"一键体验演示已识别 {len(planned)} 项事项，请确认后入库。")
    state.goto("事项智能识别")


def _render_kpi(dashboard: Mapping[str, Any]) -> None:
    """5 个核心指标卡片。"""
    cards.kpi_row(
        [
            {"label": "总任务", "value": dashboard["total"], "desc": "任务数据库中的任务总数"},
            {
                "label": "完成率",
                "value": f"{dashboard['completion_rate']}%",
                "desc": "已完成 / 总任务",
            },
            {
                "label": "临期",
                "value": dashboard["due_soon"],
                "desc": "剩余 3 天内",
                "color": styles.WARNING,
            },
            {
                "label": "紧急",
                "value": dashboard["urgent"],
                "desc": "剩余 0~1 天",
                "color": "#EA580C",
            },
            {
                "label": "逾期",
                "value": dashboard["overdue"],
                "desc": "已超过截止时间",
                "color": styles.DANGER,
            },
        ]
    )


def _render_department_ranking(dashboard: Mapping[str, Any]) -> None:
    """部门任务排行（仅柱状图，表格信息重复已移除）。"""
    cards.section("部门任务排行", "按任务数量排序")

    top_departments = dashboard["top_departments"]
    if not top_departments:
        st.caption("暂无部门数据。")
        return

    frame = pd.DataFrame(list(top_departments)).rename(columns={"department": "部门", "count": "任务数"})
    st.bar_chart(frame.set_index("部门"), color=styles.PRIMARY, height=CHART_HEIGHT)


def _render_focus_banner() -> None:
    """今日重点督办横幅：直接复用提醒与统计结果，页面不重新计算任何业务规则。"""
    counts = reminder_agent.count_levels()
    items = reminder_agent.check_all()

    cards.banner("今日重点督办", [_focus_text(counts, items)])

    if st.button("查看全部任务", type="primary", key="cf_focus_all"):
        state.goto("任务督办中心")


def _focus_text(counts: Mapping[str, int], items: Sequence[Mapping[str, Any]]) -> str:
    """把提醒数量与最优先事项组织成一句督办提示。"""
    overdue = int(counts.get("overdue") or 0)
    urgent = int(counts.get("urgent") or 0)

    if overdue == 0 and urgent == 0:
        return "当前无逾期与紧急事项，各项任务按计划有序推进。"

    segments: list[str] = []
    if overdue:
        segments.append(f"{overdue} 项逾期")
    if urgent:
        segments.append(f"{urgent} 项紧急")

    headline = next(
        (str(item.get("title")) for item in items if item.get("level") in FOCUS_LEVELS and item.get("title")),
        None,
    )
    tail = f"，建议优先处理「{headline}」等任务。" if headline else "，建议尽快跟进处理。"
    return f"当前共有 {'、'.join(segments)}事项{tail}"


def _render_suggestions(suggestions: Sequence[str]) -> None:
    """今日督办建议列表。"""
    cards.section("今日督办建议", "基于执行数据的规则化分析")

    if suggestions:
        cards.bullet_list(list(suggestions[:MAX_SUGGESTIONS_SHOWN]))
    else:
        st.caption("当前暂无督办建议。")
