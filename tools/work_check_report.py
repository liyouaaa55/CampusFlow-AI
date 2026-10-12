"""v3.0 第二阶段：工作检查报告（正式文字总结 + 完整任务明细表）。

定位：把「统一宽表 + 统一统计口径」渲染成正式报告文本，供页面展示与导出复用。
本模块**只做渲染与措辞组织**，不做任何业务判断、不访问数据库：
    数据来源：tools.execution_check（任务 × 反馈 × 下发 × 闭环的唯一口径）
    调用方   ：ui/pages/report_page.py（页面展示 + Word / Markdown 导出）

报告结构（与产品约定一致）：
    一、总体情况      统计时点 + 总体数量与执行情况（含指标表）
    二、主要问题      逾期未完成 / 未反馈 / 临期等事实统计（全部来自真实数据）
    三、重点关注任务  问题 → 依据 → 建议
    四、后续督办建议  规则化建议
    五、任务明细表    逐行对应具体任务（缺失信息显式标注，不虚构）
"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from typing import Any, Mapping, Sequence

from config import settings
from tools import execution_check

TITLE: str = "工作检查报告"

PLACEHOLDER: str = "—"
UNFEEDBACK_TEXT: str = "未反馈"

# 反馈状态中文（本模块不依赖 ui 层，故自持一份最小映射）
FEEDBACK_LABELS: dict[str, str] = {
    execution_check.FEEDBACK_NONE: UNFEEDBACK_TEXT,
    "completed": "已完成",
    "in_progress": "进行中",
}

DETAIL_COLUMNS: tuple[str, ...] = (
    "任务名称",
    "责任部门",
    "负责人",
    "截止时间",
    "任务状态",
    "最新反馈",
    "反馈说明",
    "是否逾期",
    "后续建议",
)

# 重点关注任务在正文中最多列举的条数
MAX_FOCUS_ITEMS: int = 10


def status_label(status: Any) -> str:
    """任务状态中文名（取全局展示口径）。"""
    key = str(status or "")
    return settings.STATUS_LABELS.get(key, key or PLACEHOLDER)


def feedback_label(status: Any) -> str:
    """反馈状态中文名（none 显示为「未反馈」）。"""
    return FEEDBACK_LABELS.get(str(status or execution_check.FEEDBACK_NONE), PLACEHOLDER)


def row_suggestion(row: Mapping[str, Any]) -> str:
    """单条任务的后续处理建议（确定性规则，不编造）。"""
    if row.get("closed"):
        return "已正式闭环，无需处理"
    if row.get("overdue_unfinished"):
        return "已逾期未完成：立即跟进并更新执行反馈"
    if row.get("dispatched") and row.get("feedback_status") == execution_check.FEEDBACK_NONE:
        return "已下发未反馈：提醒负责人提交执行反馈"
    days_left = row.get("days_left")
    if isinstance(days_left, int) and 0 <= days_left <= execution_check.DUE_SOON_DAYS:
        if str(row.get("status") or "") != "completed":
            return "临近截止：提前跟进，避免转为逾期"
    if str(row.get("status") or "") == "completed":
        return "已完成：可进入结果核验与正式闭环"
    return "按计划推进"


def build_detail_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """构建任务明细表（每一行对应一个具体任务）。"""
    detail: list[dict[str, str]] = []
    for row in rows:
        overdue = "是" if row.get("overdue") else "否"
        if row.get("closed"):
            overdue = "是（已闭环）" if row.get("overdue") else "否（已闭环）"
        detail.append(
            {
                "任务名称": str(row.get("title") or PLACEHOLDER),
                "责任部门": str(row.get("department") or "未指定部门"),
                "负责人": str(row.get("owner") or "未指定负责人"),
                "截止时间": str(row.get("deadline") or "未设定"),
                "任务状态": status_label(row.get("status")),
                "最新反馈": feedback_label(row.get("feedback_status")),
                "反馈说明": str(row.get("feedback_note") or "暂无记录"),
                "是否逾期": overdue,
                "后续建议": row_suggestion(row),
            }
        )
    return detail


def build_overview_paragraphs(
    overview: Mapping[str, Any], today: date, generated_at: datetime | None = None
) -> list[str]:
    """总体情况的正式文字段落（不机械罗列数字，但要给出可核对的结论）。"""
    total = int(overview.get("total") or 0)
    if not total:
        return [f"截至 {today.isoformat()}，系统内暂无任务记录，无法形成执行情况结论。"]

    first = (
        f"截至 {today.isoformat()}，本期共纳入 {total} 项任务，"
        f"其中已完成 {overview['completed']} 项（完成率 {overview['completion_rate']}%），"
        f"进行中 {overview['processing']} 项。"
    )
    second = (
        f"已进入执行阶段（已下发）{overview['dispatched']} 项，"
        f"其中 {overview['unfeedback']} 项尚未收到执行反馈。"
        f"逾期未完成 {overview['overdue_unfinished']} 项，"
        f"逾期后已完成 {overview['overdue_completed']} 项，"
        f"已正式闭环 {overview['closed']} 项。"
    )
    third_parts: list[str] = []
    if overview["overdue_unfinished"]:
        third_parts.append("存在逾期未完成事项，需优先处置")
    if overview["unfeedback"]:
        third_parts.append("存在已下发但未反馈的任务，需推动反馈")
    if not third_parts:
        third_parts.append("未发现逾期未完成或长期未反馈的情况")
    third = "；".join(third_parts) + "。"

    paragraphs = [first, second, third]
    if generated_at is not None:
        paragraphs.append(f"本报告生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}。")
    return paragraphs


def build_problem_lines(
    overview: Mapping[str, Any], supervision: Sequence[Mapping[str, Any]]
) -> list[str]:
    """主要问题：按督办类型汇总真实数量。"""
    counts: dict[str, int] = {}
    for item in supervision:
        kind = str(item.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1

    lines: list[str] = []
    if int(overview.get("overdue_unfinished") or 0):
        lines.append(f"逾期未完成 {overview['overdue_unfinished']} 项，需立即跟进。")
    if int(overview.get("unfeedback") or 0):
        lines.append(f"已下发但未反馈 {overview['unfeedback']} 项，执行进展不透明。")
    for kind in execution_check.SUPERVISION_TYPES:
        count = counts.get(kind, 0)
        if count:
            lines.append(f"{execution_check.SUPERVISION_LABELS[kind]} {count} 项。")
    if not lines:
        lines.append("本期未发现需要重点关注的问题。")

    # 去重（例如「逾期未完成」可能与督办类型重复表述）
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return unique


def build_focus_lines(supervision: Sequence[Mapping[str, Any]]) -> list[str]:
    """重点关注任务：问题 → 依据 → 建议。"""
    lines: list[str] = []
    for item in list(supervision)[:MAX_FOCUS_ITEMS]:
        lines.append(
            f"**{item.get('title') or '未命名任务'}**"
            f"（{item.get('tag')}）：{item.get('evidence')}；建议：{item.get('suggestion')}"
        )
    return lines


def build_suggestion_lines(overview: Mapping[str, Any]) -> list[str]:
    """后续督办建议（规则化，不编造）。"""
    lines: list[str] = []
    if int(overview.get("overdue_unfinished") or 0):
        lines.append("对逾期未完成事项逐项落实到人，明确补救时间并回填执行反馈。")
    if int(overview.get("unfeedback") or 0):
        lines.append("要求已下发未反馈任务的负责人在期限内提交执行反馈。")
    if int(overview.get("processing") or 0):
        lines.append("对进行中任务保持固定节奏复盘，避免临近截止集中暴露风险。")
    if int(overview.get("completed") or 0) and not int(overview.get("closed") or 0):
        lines.append("对已完成任务及时办理结果核验与正式闭环，形成可复用经验。")
    if not lines:
        lines.append("各项任务按计划推进，建议保持现有督办节奏。")
    return lines


# ============================================================
# 渲染：Markdown 与 Word
# ============================================================


def _markdown_table(detail: Sequence[Mapping[str, str]]) -> str:
    """把明细表渲染为 Markdown 表格（无数据时给出明确说明）。"""
    if not detail:
        return "_（本期无任务记录）_"
    lines = [
        "| " + " | ".join(DETAIL_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in DETAIL_COLUMNS) + " |",
    ]
    for row in detail:
        lines.append("| " + " | ".join(str(row.get(col, PLACEHOLDER)) for col in DETAIL_COLUMNS) + " |")
    return "\n".join(lines)


def render_markdown(
    *,
    rows: Sequence[Mapping[str, Any]],
    overview: Mapping[str, Any],
    supervision: Sequence[Mapping[str, Any]],
    today: date,
    generated_at: datetime | None = None,
) -> str:
    """渲染完整报告 Markdown（正文 + 完整任务明细表）。"""
    detail = build_detail_rows(rows)
    blocks: list[str] = [
        f"# {TITLE}",
        "",
        "> 统计时点：{today} ｜ 生成时间：{generated} ｜ {source} ｜ {demo}".format(
            today=today.isoformat(),
            generated=(generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
            source="CampusFlow AI 任务数据库",
            demo=settings.DEMO_DATA_LABEL,
        ),
        "",
        "## 一、总体情况",
        "",
    ]
    blocks += [f"{paragraph}" for paragraph in build_overview_paragraphs(overview, today)]
    blocks += [
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 任务总数 | {overview['total']} |",
        f"| 已下发 | {overview['dispatched']} |",
        f"| 已完成 | {overview['completed']} |",
        f"| 进行中 | {overview['processing']} |",
        f"| 未反馈 | {overview['unfeedback']} |",
        f"| 逾期未完成 | {overview['overdue_unfinished']} |",
        f"| 逾期后完成 | {overview['overdue_completed']} |",
        f"| 已正式闭环 | {overview['closed']} |",
        f"| 完成率 | {overview['completion_rate']}% |",
        "",
        "## 二、主要问题",
        "",
    ]
    blocks += [f"{index}. {line}" for index, line in enumerate(build_problem_lines(overview, supervision), 1)]
    blocks += ["", "## 三、重点关注任务", ""]
    focus = build_focus_lines(supervision)
    blocks += [f"- {line}" for line in focus] if focus else ["本期无需要重点关注的执行风险。"]
    blocks += ["", "## 四、后续督办建议", ""]
    blocks += [f"{index}. {line}" for index, line in enumerate(build_suggestion_lines(overview), 1)]
    blocks += ["", "## 五、任务明细表", "", _markdown_table(detail), ""]
    return "\n".join(blocks)


def render_docx(
    *,
    rows: Sequence[Mapping[str, Any]],
    overview: Mapping[str, Any],
    supervision: Sequence[Mapping[str, Any]],
    today: date,
    generated_at: datetime | None = None,
) -> bytes:
    """渲染 Word（.docx）报告，返回字节内容。

    说明：python-docx 已是项目既有依赖（用于 Word 材料解析），
    因此这里不新增任何第三方依赖，也没有网络或外部服务调用。
    """
    from docx import Document  # 延迟导入，避免未安装时影响页面其他功能

    document = Document()
    document.add_heading(TITLE, level=0)
    document.add_paragraph(
        f"统计时点：{today.isoformat()}　生成时间："
        f"{(generated_at or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')}　"
        f"{settings.DEMO_DATA_LABEL}"
    )

    document.add_heading("一、总体情况", level=1)
    for paragraph in build_overview_paragraphs(overview, today):
        document.add_paragraph(paragraph)

    metrics = [
        ("任务总数", overview["total"]),
        ("已下发", overview["dispatched"]),
        ("已完成", overview["completed"]),
        ("进行中", overview["processing"]),
        ("未反馈", overview["unfeedback"]),
        ("逾期未完成", overview["overdue_unfinished"]),
        ("逾期后完成", overview["overdue_completed"]),
        ("已正式闭环", overview["closed"]),
        ("完成率", f"{overview['completion_rate']}%"),
    ]
    table = document.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.rows[0].cells[0].text = "指标"
    table.rows[0].cells[1].text = "数值"
    for name, value in metrics:
        cells = table.add_row().cells
        cells[0].text = str(name)
        cells[1].text = str(value)

    document.add_heading("二、主要问题", level=1)
    for line in build_problem_lines(overview, supervision):
        document.add_paragraph(line, style="List Number")

    document.add_heading("三、重点关注任务", level=1)
    focus = build_focus_lines(supervision)
    if not focus:
        document.add_paragraph("本期无需要重点关注的执行风险。")
    else:
        for line in focus:
            document.add_paragraph(line.replace("**", ""), style="List Bullet")

    document.add_heading("四、后续督办建议", level=1)
    for line in build_suggestion_lines(overview):
        document.add_paragraph(line, style="List Number")

    document.add_heading("五、任务明细表", level=1)
    detail = build_detail_rows(rows)
    if not detail:
        document.add_paragraph("本期无任务记录。")
    else:
        table = document.add_table(rows=1, cols=len(DETAIL_COLUMNS))
        table.style = "Table Grid"
        for index, column in enumerate(DETAIL_COLUMNS):
            table.rows[0].cells[index].text = column
        for row in detail:
            cells = table.add_row().cells
            for index, column in enumerate(DETAIL_COLUMNS):
                cells[index].text = str(row.get(column, PLACEHOLDER))

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_report(
    *,
    today: date | None = None,
    generated_at: datetime | None = None,
    db_path: Any = None,
) -> dict[str, Any]:
    """从统一口径取数并生成报告（页面与导出共用的唯一入口）。"""
    base_date = today or date.today()
    moment = generated_at or datetime.now()
    rows = execution_check.build_task_rows(today=base_date, db_path=db_path)
    overview = execution_check.build_overview(rows)
    supervision = execution_check.supervision_items(today=base_date, rows=rows)
    return {
        "rows": rows,
        "overview": overview,
        "supervision": supervision,
        "detail": build_detail_rows(rows),
        "markdown": render_markdown(
            rows=rows, overview=overview, supervision=supervision,
            today=base_date, generated_at=moment,
        ),
        "docx": render_docx(
            rows=rows, overview=overview, supervision=supervision,
            today=base_date, generated_at=moment,
        ),
        "today": base_date,
        "generated_at": moment,
    }
