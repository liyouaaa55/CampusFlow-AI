"""渲染工具：把统计与提醒数据渲染为 Markdown 文本。

定位：只做「数据结构 → Markdown 字符串」的转换，不含任何业务判断，也不依赖
agent / database 层。因此本模块可被 Dashboard、部门报告、周报告与导出功能共同复用。

约定：
- 级别中文名等展示信息由调用方传入（来自 knowledge/rules/reminder.json）；
- 任务状态与优先级的中文名取自 config/settings.py（全局展示口径统一）；
- 所有报告头部都带「演示数据（Synthetic Data）」标注。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from config import settings

# 缺省级别显示名：仅在调用方未提供 labels 时兜底。
#
# 权威来源是 knowledge/rules/reminder.json 的 label（由 reminder_agent.level_label()
# 读取）。此处保留一份静态副本，只为在知识库文件缺失时仍能渲染报告；
# tests/test_ui_labels.py 会断言本表与 reminder.json 完全一致，防止两处漂移。
DEFAULT_LEVEL_LABELS: dict[str, str] = {
    "normal": "正常",
    "due_soon": "即将到期",
    "urgent": "临期",
    "overdue": "已逾期",
}

# 空数据提示
EMPTY_REMINDER_TEXT: str = "暂无需要提醒的任务。"
EMPTY_RISK_TEXT: str = "暂无临期或已逾期事项。"
EMPTY_TASK_TEXT: str = "暂无任务记录。"

# 报告数据来源说明
DEFAULT_SOURCE_NOTE: str = "CampusFlow AI Agent Memory"

# 缺失值占位符（Markdown 表格中避免出现 None）
PLACEHOLDER: str = "—"


# ============================================================
# 一、格式化辅助
# ============================================================


def format_percent(value: float) -> str:
    """百分比格式化：33.333… → 33.3%。"""
    return f"{float(value):.1f}%"


def format_timestamp(value: datetime | None = None) -> str:
    """时间格式化：YYYY-MM-DD HH:MM:SS（默认取当前时间）。"""
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def render_report_header(
    title: str,
    generated_at: datetime | None = None,
    source_note: str = DEFAULT_SOURCE_NOTE,
) -> str:
    """渲染报告头部：一级标题 + 生成时间 + 数据来源 + 演示数据标注。"""
    notes = [
        f"生成时间：{format_timestamp(generated_at)}",
        f"数据来源：{source_note}",
        settings.DEMO_DATA_LABEL,
    ]
    return "\n".join([f"# {title}", "", "> " + " ｜ ".join(notes)])


# ============================================================
# 二、表格渲染
# ============================================================


def render_metrics_table(metrics: Mapping[str, Any]) -> str:
    """渲染总体概况表：指标 | 数值。"""
    lines = ["| 指标 | 数值 |", "| --- | ---: |"]
    for name, value in metrics.items():
        lines.append(f"| {name} | {value} |")
    return "\n".join(lines)


def render_department_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """渲染部门统计表：部门 | 总数 | 完成 | 完成率。"""
    lines = ["| 部门 | 总数 | 完成 | 完成率 |", "| --- | ---: | ---: | ---: |"]
    for row in rows:
        lines.append(
            "| {department} | {total} | {completed} | {rate} |".format(
                department=row.get("department") or PLACEHOLDER,
                total=int(row.get("total") or 0),
                completed=int(row.get("completed") or 0),
                rate=format_percent(row.get("completion_rate") or 0.0),
            )
        )
    return "\n".join(lines)


def render_task_table(tasks: Sequence[Mapping[str, Any]]) -> str:
    """渲染任务明细表：标题 | 状态 | 负责人 | 截止时间 | 优先级。"""
    lines = ["| 标题 | 状态 | 负责人 | 截止时间 | 优先级 |", "| --- | --- | --- | --- | --- |"]
    for task in tasks:
        status = str(task.get("status") or "")
        priority = str(task.get("priority") or "")
        lines.append(
            "| {title} | {status} | {owner} | {deadline} | {priority} |".format(
                title=task.get("title") or PLACEHOLDER,
                status=settings.STATUS_LABELS.get(status, status or PLACEHOLDER),
                owner=task.get("owner") or PLACEHOLDER,
                deadline=task.get("deadline") or PLACEHOLDER,
                priority=settings.PRIORITY_LABELS.get(priority, priority or PLACEHOLDER),
            )
        )
    return "\n".join(lines)


# ============================================================
# 三、列表渲染
# ============================================================


def render_bullet_list(items: Sequence[str], empty_text: str = "") -> str:
    """渲染无序列表（- ），列表为空时返回 empty_text。"""
    if not items:
        return empty_text
    return "\n".join(f"- {item}" for item in items)


def render_suggestions(items: Sequence[str]) -> str:
    """渲染 AI 分析建议（有序列表）。"""
    if not items:
        return ""
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def render_level_counts(
    counts: Mapping[str, int],
    labels: Mapping[str, str] | None = None,
    title: str = "提醒级别统计",
) -> str:
    """把 count_levels() 的结果渲染为 Markdown 表格（含合计行）。"""
    label_map = dict(DEFAULT_LEVEL_LABELS)
    if labels:
        label_map.update(labels)

    lines: list[str] = [f"**{title}**", "", "| 级别 | 数量 |", "| --- | ---: |"]
    total = 0
    for level, count in counts.items():
        number = int(count)
        total += number
        lines.append(f"| {label_map.get(level, level)} | {number} |")
    lines.append(f"| 合计 | {total} |")
    return "\n".join(lines)


def render_reminders(
    items: Sequence[Mapping[str, Any]],
    labels: Mapping[str, str] | None = None,
) -> str:
    """把提醒对象列表渲染为 Markdown 列表。

    每行格式：- **任务标题**（级别标签，剩余/逾期天数）：提醒文案（负责人 / 部门）
    """
    if not items:
        return EMPTY_REMINDER_TEXT

    label_map = dict(DEFAULT_LEVEL_LABELS)
    if labels:
        label_map.update(labels)

    lines: list[str] = []
    for item in items:
        level = str(item.get("level", ""))
        label = label_map.get(level, level)
        days_left = int(item.get("days_left") or 0)
        days_text = f"逾期 {abs(days_left)} 天" if days_left < 0 else f"剩余 {days_left} 天"
        owner = item.get("owner") or "未指定"
        department = item.get("department") or "未指定"
        message = item.get("message") or ""

        lines.append(
            f"- **{item.get('title') or '未命名任务'}**（{label}，{days_text}）：{message}"
            f"（负责人：{owner}；部门：{department}）"
        )
    return "\n".join(lines)
