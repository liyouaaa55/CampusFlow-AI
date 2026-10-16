"""ReportAgent：管理驾驶舱与阶段性报告（纯规则，不调用 LLM）。

职责：把 Agent Memory 中的任务数据整理成管理者可读的统计与报告。

**数据来源严格受限**（禁止重算提醒等级）：
- database.sqlite_db.list_tasks()          任务明细与状态统计
- agent.reminder_agent.count_levels()      临期 / 紧急 / 逾期数量
- agent.reminder_agent.generate_messages() 风险事项文本

本模块不做任何日期计算、不判断提醒等级，全部沿用 ReminderAgent 的输出。

接口：
    generate_dashboard()              返回统计字典（供 Dashboard 卡片与图表使用）
    generate_weekly_report()          返回本周报告 Markdown
    generate_department_report(dept)  返回部门报告 Markdown

报告中的「AI分析建议」由固定规则生成（见 build_suggestions），共 3~5 条。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent import reminder_agent
from database import sqlite_db
from knowledge import KnowledgeError, load_template_json
from tools import report_tool

# ============================================================
# 一、常量与规则
# ============================================================

# 报告标题（内置默认值；若 knowledge/templates/report.json 提供同名字段则以其为准）
WEEKLY_REPORT_TITLE: str = "本周事务闭环执行报告"
DEPARTMENT_REPORT_TITLE_TEMPLATE: str = "{department} 事务闭环执行报告"

# 报告结构模板文件名：knowledge/templates/report.json
REPORT_TEMPLATE_NAME: str = "report.json"

# 章节标题内置默认值：模板缺失或某个键缺失时逐项回退，保证报告永远可生成
DEFAULT_SECTION_TITLES: dict[str, str] = {
    "overview": "一、总体概况",
    "departments": "二、部门统计",
    "tasks": "二、任务明细",
    "risk": "三、风险事项（仅临期与已逾期）",
    "suggestions": "四、AI分析建议",
}

# 风险事项仅列这两类（来自 ReminderAgent 的级别定义）
RISK_LEVELS: tuple[str, ...] = ("urgent", "overdue")

# Dashboard 默认展示前 3 个任务量最多的部门
DASHBOARD_TOP_N: int = 3

# 完成率警戒线：低于该值提示加强督办（需求给定规则）
COMPLETION_RATE_THRESHOLD: float = 70.0

# 紧急事项超过该数量时提示召开专项协调会（需求给定规则）
URGENT_MEETING_THRESHOLD: int = 3

# AI 分析建议条数区间（需求要求 3~5 条）
MIN_SUGGESTIONS: int = 3
MAX_SUGGESTIONS: int = 5

# ---------- 建议文案（纯规则，禁止 LLM）----------
MSG_OVERDUE: str = "建议优先处理已逾期事项。"
MSG_COORDINATION: str = "建议召开专项协调会。"
MSG_SUPERVISE: str = "建议加强督办力度。"
MSG_URGENT_FOLLOWUP: str = "建议及时跟进 {count} 项临期事项。"
MSG_DUE_SOON: str = "建议提前安排 {count} 项即将到期事项，避免转为已逾期。"
MSG_RATE_OK: str = "完成率 {rate} 已达目标，建议保持当前推进节奏。"
MSG_RATE_PERFECT: str = "完成率 100%，建议总结本期经验并沉淀标准流程。"
MSG_MISSING_OWNER: str = "建议为 {count} 项任务补充负责人。"
MSG_MISSING_DEPARTMENT: str = "建议为 {count} 项任务补充责任部门。"
MSG_NEED_CONFIRM: str = "建议尽快完成 {count} 项任务的人工确认。"
MSG_NO_DATA: str = "当前暂无任务数据，建议先上传会议纪要或台账文档识别事项。"

# 通用建议：仅用于补足「至少 3 条」的下限，放在规则建议之后
FILLER_SUGGESTIONS: tuple[str, ...] = (
    "建议保持每周固定复盘节奏，确保事项闭环。",
    "建议对已完成事项及时归档，形成可复用经验。",
)

# Dashboard 输出字段顺序（与需求文档一致）
DASHBOARD_FIELDS: tuple[str, ...] = (
    "total",
    "completed",
    "processing",
    "completion_rate",
    "normal",
    "due_soon",
    "urgent",
    "overdue",
    "top_departments",
)


# ============================================================
# 二、对外接口
# ============================================================


def generate_dashboard(
    top_n: int = DASHBOARD_TOP_N,
    today: Any = None,
    db_path: str | Path | None = None,
    *,
    exclude_task_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """生成驾驶舱统计（供 Dashboard 使用）。

    返回固定字段：
        {"total": 20, "completed": 12, "processing": 5, "completion_rate": 60.0,
         "normal": 15, "due_soon": 2, "urgent": 2, "overdue": 1,
         "top_departments": [{"department": "学工办", "count": 6}, ...]}

    说明：completed / processing 为对应状态的任务数（按状态字面统计）；
    normal / due_soon / urgent / overdue 直接取 count_levels()，其中已完成任务
    不参与等级统计（由 ReminderAgent 决定），因此四项之和可能小于 total。

    exclude_task_ids：等级统计额外排除的任务 ID（如正式闭环任务，由调用方
    以 task_closures 为权威来源传入）；total / completed / processing 等
    普查口径不受影响。缺省 None 时行为与既有版本完全一致。
    """
    stats = build_stats(today=today, db_path=db_path, exclude_task_ids=exclude_task_ids)
    return {
        "total": stats["total"],
        "completed": stats["completed"],
        "processing": stats["processing"],
        "completion_rate": stats["completion_rate"],
        "normal": stats["normal"],
        "due_soon": stats["due_soon"],
        "urgent": stats["urgent"],
        "overdue": stats["overdue"],
        "top_departments": top_departments(stats["tasks"], top_n=top_n),
    }


def generate_weekly_report(
    today: Any = None,
    db_path: str | Path | None = None,
    generated_at: datetime | None = None,
) -> str:
    """生成本周事务闭环执行报告（Markdown 文本）。

    部门统计列出全部部门（不做截断），保证报告数据完整。
    """
    stats = build_stats(today=today, db_path=db_path)
    risk = risk_messages(today=today, db_path=db_path)
    suggestions = build_suggestions(stats)
    titles = _section_titles()

    return _assemble_report(
        title=_template_title("weekly_title", WEEKLY_REPORT_TITLE),
        generated_at=generated_at,
        stats=stats,
        second_section_title=titles["departments"],
        second_section_body=report_tool.render_department_table(stats["departments"]),
        unassigned_note=_unassigned_note(stats),
        risk=risk,
        suggestions=suggestions,
        section_titles=titles,
    )


def generate_department_report(
    department: str,
    today: Any = None,
    db_path: str | Path | None = None,
    generated_at: datetime | None = None,
) -> str:
    """生成指定部门的事务闭环执行报告（Markdown 文本）。

    未知部门同样可以生成报告：统计为 0，并给出数据提示。
    """
    stats = build_stats(department=department, today=today, db_path=db_path)
    risk = risk_messages(department=department, today=today, db_path=db_path)
    suggestions = build_suggestions(stats)
    titles = _section_titles()

    return _assemble_report(
        title=_template_title("department_title", DEPARTMENT_REPORT_TITLE_TEMPLATE).format(
            department=department
        ),
        generated_at=generated_at,
        stats=stats,
        second_section_title=titles["tasks"],
        second_section_body=report_tool.render_task_table(stats["tasks"]),
        unassigned_note="",
        risk=risk,
        suggestions=suggestions,
        section_titles=titles,
    )


# ============================================================
# 三、统计（唯一数据来源：list_tasks + count_levels）
# ============================================================


def build_stats(
    department: str | None = None,
    today: Any = None,
    db_path: str | Path | None = None,
    *,
    exclude_task_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """聚合统计：任务明细来自 list_tasks()，提醒等级来自 count_levels()。

    exclude_task_ids 只作用于提醒等级统计（排除正式闭环任务等），
    任务明细与 completed / processing 等普查口径保持全量。
    """
    tasks = sqlite_db.list_tasks(department=department, db_path=db_path)
    levels = reminder_agent.count_levels(
        department=department, today=today, db_path=db_path, exclude_task_ids=exclude_task_ids
    )

    total = len(tasks)
    completed = _count_by_status(tasks, "completed")
    processing = _count_by_status(tasks, "processing")

    return {
        "total": total,
        "completed": completed,
        "processing": processing,
        # 完成率保留一位小数，避免出现 33.333333
        "completion_rate": round(completed * 100 / total, 1) if total else 0.0,
        "normal": levels["normal"],
        "due_soon": levels["due_soon"],
        "urgent": levels["urgent"],
        "overdue": levels["overdue"],
        # 信息缺失统计（用于生成建议，不影响提醒等级）
        "missing_owner": sum(1 for task in tasks if not task.get("owner")),
        "missing_department": sum(1 for task in tasks if not task.get("department")),
        "need_confirm": sum(1 for task in tasks if task.get("need_confirm")),
        # 明细与部门统计
        "tasks": tasks,
        "departments": department_stats(tasks),
    }


def department_stats(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按部门统计：总数、完成数、完成率。

    排序：总数降序 → 部门名称升序（保证结果稳定）。
    未指定部门的任务不计入部门统计（会在报告中单独说明）。
    """
    grouped: dict[str, dict[str, int]] = {}
    for task in tasks:
        department = (task.get("department") or "").strip()
        if not department:
            continue
        bucket = grouped.setdefault(department, {"total": 0, "completed": 0})
        bucket["total"] += 1
        if task.get("status") == "completed":
            bucket["completed"] += 1

    rows: list[dict[str, Any]] = [
        {
            "department": department,
            "total": bucket["total"],
            "completed": bucket["completed"],
            "completion_rate": round(bucket["completed"] * 100 / bucket["total"], 1),
        }
        for department, bucket in grouped.items()
    ]
    rows.sort(key=lambda row: (-row["total"], row["department"]))
    return rows


def top_departments(
    tasks: Sequence[Mapping[str, Any]],
    top_n: int = DASHBOARD_TOP_N,
) -> list[dict[str, Any]]:
    """取任务量最多的前 N 个部门，返回 [{"department": ..., "count": ...}]。"""
    rows = department_stats(tasks)
    return [{"department": row["department"], "count": row["total"]} for row in rows[: max(top_n, 0)]]


def risk_messages(
    department: str | None = None,
    today: Any = None,
    db_path: str | Path | None = None,
) -> list[str]:
    """取「紧急 / 已逾期」的提醒文本。

    直接复用 ReminderAgent.generate_messages() 的输出，仅按级别标签筛选，
    不重新计算任何提醒等级。
    """
    messages = reminder_agent.generate_messages(
        department=department, today=today, db_path=db_path
    )
    prefixes = tuple(f"【{reminder_agent.level_label(level)}】" for level in RISK_LEVELS)
    return [message for message in messages if message.startswith(prefixes)]


# ============================================================
# 四、AI 分析建议（纯规则，3~5 条）
# ============================================================


def build_suggestions(stats: Mapping[str, Any]) -> list[str]:
    """根据统计生成 3~5 条建议。

    规则（全部确定性，不调用 LLM）：
    1. 逾期 > 0        → 建议优先处理逾期事项。
    2. 紧急 > 3        → 建议召开专项协调会；0 < 紧急 ≤ 3 → 建议及时跟进。
    3. 完成率 < 70%    → 建议加强督办力度；否则给出达标/满分提示。
    4. 补充规则：临期、负责人缺失、部门缺失、待人工确认。
    5. 不足 3 条时用通用建议补齐；超过 5 条时按优先级截断。
    """
    total = int(stats.get("total") or 0)
    overdue = int(stats.get("overdue") or 0)
    urgent = int(stats.get("urgent") or 0)
    rate = float(stats.get("completion_rate") or 0.0)

    suggestions: list[str] = []

    if total == 0:
        suggestions.append(MSG_NO_DATA)

    # 规则 1：逾期
    if overdue > 0:
        suggestions.append(MSG_OVERDUE)

    # 规则 2：紧急
    if urgent > URGENT_MEETING_THRESHOLD:
        suggestions.append(MSG_COORDINATION)
    elif urgent > 0:
        suggestions.append(MSG_URGENT_FOLLOWUP.format(count=urgent))

    # 规则 3：完成率
    if total > 0:
        if rate < COMPLETION_RATE_THRESHOLD:
            suggestions.append(MSG_SUPERVISE)
        elif rate >= 100.0:
            suggestions.append(MSG_RATE_PERFECT)
        else:
            suggestions.append(MSG_RATE_OK.format(rate=report_tool.format_percent(rate)))

    # 规则 4：临期与信息缺失
    due_soon = int(stats.get("due_soon") or 0)
    if due_soon > 0:
        suggestions.append(MSG_DUE_SOON.format(count=due_soon))
    if int(stats.get("missing_owner") or 0) > 0:
        suggestions.append(MSG_MISSING_OWNER.format(count=int(stats["missing_owner"])))
    if int(stats.get("missing_department") or 0) > 0:
        suggestions.append(MSG_MISSING_DEPARTMENT.format(count=int(stats["missing_department"])))
    if int(stats.get("need_confirm") or 0) > 0:
        suggestions.append(MSG_NEED_CONFIRM.format(count=int(stats["need_confirm"])))

    # 补足下限（保证 3~5 条）
    for filler in FILLER_SUGGESTIONS:
        if len(suggestions) >= MIN_SUGGESTIONS:
            break
        suggestions.append(filler)

    return suggestions[:MAX_SUGGESTIONS]


# ============================================================
# 五、报告结构模板（knowledge/templates/report.json）
# ============================================================


def load_report_template(force_reload: bool = False) -> dict[str, Any]:
    """读取报告结构模板。

    报告模板属于「展示层增强」：文件缺失、结构损坏都不是错误，
    一律返回空字典，由调用方逐项回退到内置标题，保证报告永远可生成。
    """
    try:
        return load_template_json(REPORT_TEMPLATE_NAME, force_reload=force_reload)
    except KnowledgeError:
        return {}


def _template_title(key: str, default: str) -> str:
    """取模板中的标题；缺失或空白时回退内置默认值。"""
    value = str(load_report_template().get(key) or "").strip()
    return value or default


def _section_titles() -> dict[str, str]:
    """取模板中的章节标题，逐项回退内置默认值。"""
    sections = load_report_template().get("sections")
    raw: Mapping[str, Any] = sections if isinstance(sections, Mapping) else {}

    titles: dict[str, str] = {}
    for key, default in DEFAULT_SECTION_TITLES.items():
        value = str(raw.get(key) or "").strip()
        titles[key] = value or default
    return titles


# ============================================================
# 六、内部实现
# ============================================================


def _assemble_report(
    title: str,
    generated_at: datetime | None,
    stats: Mapping[str, Any],
    second_section_title: str,
    second_section_body: str,
    unassigned_note: str,
    risk: Sequence[str],
    suggestions: Sequence[str],
    section_titles: Mapping[str, str] | None = None,
) -> str:
    """按统一框架拼装 Markdown 报告（周报与部门报告共用）。"""
    titles = dict(DEFAULT_SECTION_TITLES)
    titles.update(section_titles or {})

    blocks: list[str] = [
        report_tool.render_report_header(title, generated_at=generated_at),
        "",
        f"## {titles['overview']}",
        "",
        report_tool.render_metrics_table(_overview_metrics(stats)),
        "",
        f"## {second_section_title}",
        "",
        second_section_body,
    ]

    if unassigned_note:
        blocks += ["", unassigned_note]

    blocks += [
        "",
        f"## {titles['risk']}",
        "",
        report_tool.render_bullet_list(list(risk), report_tool.EMPTY_RISK_TEXT),
        "",
        f"## {titles['suggestions']}",
        "",
        report_tool.render_suggestions(list(suggestions)),
    ]

    return "\n".join(blocks)


def _overview_metrics(stats: Mapping[str, Any]) -> dict[str, Any]:
    """总体概况的指标表内容。

    提醒等级的中文名统一取自 knowledge/rules/reminder.json（经
    reminder_agent.level_label() 读取），不在此处硬编码，避免与界面口径漂移。
    """
    return {
        "总任务": int(stats.get("total") or 0),
        "已完成": int(stats.get("completed") or 0),
        "进行中": int(stats.get("processing") or 0),
        "完成率": report_tool.format_percent(stats.get("completion_rate") or 0.0),
        reminder_agent.level_label("due_soon"): int(stats.get("due_soon") or 0),
        reminder_agent.level_label("urgent"): int(stats.get("urgent") or 0),
        reminder_agent.level_label("overdue"): int(stats.get("overdue") or 0),
    }


def _unassigned_note(stats: Mapping[str, Any]) -> str:
    """未指定责任部门的说明行（仅当存在这类任务时返回）。"""
    count = int(stats.get("missing_department") or 0)
    if count <= 0:
        return ""
    return f"> 另有 {count} 项任务未指定责任部门，建议补充后纳入部门统计。"


def _count_by_status(tasks: Sequence[Mapping[str, Any]], status: str) -> int:
    """按状态计数。"""
    return sum(1 for task in tasks if task.get("status") == status)
