"""页面五：工作检查报告（v3.0 第二阶段）。

报告形态（与产品约定一致）：**正式文字总结 + 完整任务明细表**。

数据来源（第四页与第五页共用同一口径，避免两套互相矛盾的判断）：
    tools.work_check_report.build_report()
        ├── tools.execution_check.build_task_rows()   任务 × 反馈 × 下发 × 闭环 宽表
        ├── tools.execution_check.build_overview()    统一统计口径
        └── tools.execution_check.supervision_items() 持续督办清单

统计口径（唯一，详见 tools/execution_check 模块说明）：
    任务总数 / 已下发 / 已完成 / 进行中 / 未反馈 / 逾期未完成 / 逾期后完成 / 已正式闭环
    —— 「有反馈」不等于「已完成」；「已完成」不等于「已正式闭环」。

导出：Word（.docx，优先）与 Markdown（保留），二者与页面内容同源，均包含正文与明细表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

import pandas as pd
import streamlit as st

from database import sqlite_db
from tools import work_check_report
from ui.components import cards, styles

# 页面标题与副标题
PAGE_TITLE: str = "工作检查报告"
PAGE_SUBTITLE: str = "基于最新任务、执行反馈与督办数据，生成正式文字总结与完整任务明细表"

# 标题右侧状态标签
PAGE_TAG: str = "正式报告 · 可直接汇报"

# 导出文件名
MD_FILENAME: str = "CampusFlow_工作检查报告.md"
DOCX_FILENAME: str = "CampusFlow_工作检查报告.docx"

# 报告滚动容器高度
REPORT_VIEW_HEIGHT: int = 620

# 「重新生成」后的提示文案（存 session_state 以跨 rerun 展示）
NOTE_KEY: str = "cf_work_check_note"
NOTE_TEXT: str = "报告已根据最新数据重新生成。"

# Word 导出的 MIME
DOCX_MIME: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def render() -> None:
    """渲染工作检查报告页面。"""
    cards.page_header(PAGE_TITLE, PAGE_SUBTITLE, tag=PAGE_TAG)

    note = st.session_state.pop(NOTE_KEY, None)
    if note:
        st.success(note)

    try:
        report = work_check_report.build_report(generated_at=datetime.now())
    except sqlite_db.DatabaseError:
        st.error("任务数据读取失败，请稍后重试。")
        return

    overview = report["overview"]
    _render_kpis(overview)

    st.write("")
    _render_detail_table(report["detail"])

    st.write("")
    _render_report_body(report)


# ------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------


def _render_kpis(overview: Mapping[str, Any]) -> None:
    """关键指标（与报告正文同源，不做二次计算）。"""
    cards.kpi_row(
        [
            {"label": "任务总数", "value": overview["total"], "color": styles.PRIMARY},
            {
                "label": "已完成",
                "value": f"{overview['completed']}（{overview['completion_rate']}%）",
                "color": styles.SUCCESS,
            },
            {"label": "进行中", "value": overview["processing"], "color": styles.PRIMARY},
            {
                "label": "未反馈",
                "value": overview["unfeedback"],
                "desc": "已下发但尚未收到执行反馈",
                "color": styles.WARNING,
            },
            {
                "label": "逾期未完成",
                "value": overview["overdue_unfinished"],
                "color": styles.DANGER,
            },
            {
                "label": "已正式闭环",
                "value": overview["closed"],
                "desc": "以正式闭环记录为准",
                "color": styles.SUCCESS,
            },
        ]
    )


def _render_detail_table(detail: Sequence[Mapping[str, str]]) -> None:
    """完整任务明细表（页面可读版；导出文档中含同源表格）。"""
    cards.section("任务明细表", "逐行对应具体任务；缺失信息显式标注，不作推测")

    if not detail:
        cards.empty_state(
            "暂无任务数据",
            "请先在「会议材料与任务生成」中确认任务，并在「任务督办中心」完成下发。",
        )
        return

    frame = pd.DataFrame(list(detail), columns=list(work_check_report.DETAIL_COLUMNS))
    st.dataframe(frame, hide_index=True, use_container_width=True)


def _render_report_body(report: Mapping[str, Any]) -> None:
    """报告正文（正式文字总结 + 明细表）+ 导出。"""
    cards.section("报告正文", "正式文字总结 + 完整任务明细表，内容与导出文件一致")

    generate_col, docx_col, md_col = st.columns([1, 1, 1])
    with generate_col:
        if st.button("重新生成", use_container_width=True):
            st.session_state[NOTE_KEY] = NOTE_TEXT
            st.rerun()

    markdown = str(report["markdown"])
    with md_col:
        st.download_button(
            "导出 Markdown",
            data=markdown.encode("utf-8"),
            file_name=MD_FILENAME,
            mime="text/markdown",
            use_container_width=True,
        )

    with docx_col:
        docx_bytes = report.get("docx")
        if isinstance(docx_bytes, (bytes, bytearray)) and docx_bytes:
            st.download_button(
                "导出 Word",
                data=bytes(docx_bytes),
                file_name=DOCX_FILENAME,
                mime=DOCX_MIME,
                use_container_width=True,
            )
        else:
            # Word 生成失败不得伪装成功：明确说明并保留 Markdown 导出
            st.caption("Word 报告生成不可用，请使用 Markdown 导出。")

    with st.container(height=REPORT_VIEW_HEIGHT):
        st.markdown(markdown)

    cards.note("本报告基于演示数据（Synthetic Data）自动生成，仅用于比赛展示。")
