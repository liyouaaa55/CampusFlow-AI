"""PlannerAgent：任务规划（规则 + 轻量推理，不调用 LLM）。

输入：TaskAgent 输出的任务列表（JSON 结构），字段为
      title / department / owner / deadline / priority / status / description
输出：在原字段顺序基础上追加三个规划字段
      risk_level（green / yellow / red）
      need_confirm（是否需要人工确认）
      suggestions（字符串数组，给出可执行的补全建议）

判定规则（完全确定性，便于人工复核）：
1. 优先级：任务自带且合法则沿用；缺失或非法则按 knowledge/rules/priority.json
   的关键词规则推断（依据「标题 + 描述」）。
2. 风险等级：
   - red    截止时间缺失（含格式非法）或截止时间已过期
   - yellow 负责人缺失 或 优先级缺失/非法
   - green  负责人、截止时间、优先级均齐备
   red 优先级高于 yellow（两者同时成立时取 red）。
3. need_confirm：负责人缺失 / 截止时间缺失 / 优先级缺失，任一成立即为 True。

本模块不修改 TaskAgent 的任何行为，只做二次加工。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# 复用 TaskAgent 的字段顺序定义，保证「保持原字段顺序」
from agent.task_agent import TASK_FIELDS
from config import settings
from tools import priority_tool

# PlannerAgent 新增字段（顺序固定：risk_level → need_confirm → suggestions）
PLANNER_FIELDS: tuple[str, ...] = ("risk_level", "need_confirm", "suggestions")

# 完整输出字段顺序
OUTPUT_FIELDS: tuple[str, ...] = TASK_FIELDS + PLANNER_FIELDS

# 风险等级取值
RISK_LEVELS: tuple[str, ...] = ("green", "yellow", "red")
RISK_GREEN: str = "green"
RISK_YELLOW: str = "yellow"
RISK_RED: str = "red"

# 建议文案（集中定义，便于统一措辞与测试断言）
MSG_OWNER_MISSING: str = "负责人缺失，请补充负责人"
MSG_DEADLINE_MISSING: str = "截止时间缺失，请补充截止时间"
MSG_DEADLINE_OVERDUE: str = "截止时间已过期，请确认是否需要调整"
MSG_DEADLINE_DUE_SOON: str = f"截止时间临近（{settings.REMINDER_DUE_SOON_DAYS} 天内），建议优先安排"
MSG_PRIORITY_MISSING: str = "优先级缺失，已按规则推断为 {level}，请人工确认"
MSG_HIGH_RISK_KEYWORD: str = "命中高风险关键词「{keyword}」，建议优先处理"

# 截止时间允许的写法：2026-09-30 / 2026/9/30 / 2026.9.30 / 2026年9月30日
_DEADLINE_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


def plan(
    tasks: list[dict[str, Any]] | dict[str, Any] | None,
    rules: dict[str, list[str]] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """对任务列表做整体规划。

    参数：
        tasks   TaskAgent 输出的任务列表；也兼容单条 dict；None 视为空
        rules   优先级规则，默认从 knowledge/rules/priority.json 读取
        today   基准日期（默认今天），显式传入便于测试与补算历史数据
    返回：
        规划后的任务列表，字段顺序见 OUTPUT_FIELDS
    异常：
        TypeError   元素不是字典
        ValueError  任务缺少标题
        priority_tool.RulesError  规则文件缺失或格式错误
    """
    if tasks is None:
        return []
    if isinstance(tasks, dict):
        tasks = [tasks]
    if not isinstance(tasks, list):
        raise TypeError(f"plan() 只接受列表或单条字典，收到：{type(tasks).__name__}")

    return [plan_task(task, rules=rules, today=today) for task in tasks]


def plan_task(
    task: dict[str, Any],
    rules: dict[str, list[str]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """对单条任务做规划，返回统一结构（不修改入参）。"""
    if not isinstance(task, dict):
        raise TypeError(f"任务必须是字典，收到：{type(task).__name__}")

    base_date = today or date.today()

    # ---------- 1. 字段清洗 ----------
    title = _clean_text(task.get("title"))
    if not title:
        raise ValueError("任务缺少标题，无法规划。")

    description = _clean_text(task.get("description"))
    owner = _clean_text(task.get("owner"))

    # 截止时间：解析为 date 对象；缺失或格式非法均为 None（按缺失处理）
    deadline_value = _parse_deadline(task.get("deadline"))
    deadline_text = deadline_value.isoformat() if deadline_value else None

    # 优先级：自带且合法则沿用，否则按关键词规则推断
    priority = _normalize_priority(task.get("priority"))
    priority_missing = priority is None
    if priority_missing:
        priority = priority_tool.score_by_keywords(
            _match_text(title, description), rules=rules
        )

    # 状态：非法或缺失统一回落为默认状态
    status = _clean_text(task.get("status"))
    if status not in settings.TASK_STATUSES:
        status = settings.DEFAULT_STATUS

    # ---------- 2. 风险判定 ----------
    owner_missing = owner is None
    deadline_missing = deadline_value is None
    overdue = deadline_value is not None and deadline_value < base_date

    if deadline_missing or overdue:
        risk_level = RISK_RED
    elif owner_missing or priority_missing:
        risk_level = RISK_YELLOW
    else:
        risk_level = RISK_GREEN

    # ---------- 3. 人工确认标记 ----------
    # 严格按需求定义：负责人缺失 / 截止时间缺失 / 优先级缺失，任一成立即为 True。
    # 注意：仅「已逾期」（字段齐备）不会触发 need_confirm，该风险由 risk_level=red
    # 与 suggestions 中的逾期提示传达。
    need_confirm = owner_missing or deadline_missing or priority_missing

    # ---------- 4. 建议生成 ----------
    suggestions = _build_suggestions(
        owner_missing=owner_missing,
        deadline_missing=deadline_missing,
        overdue=overdue,
        days_left=(deadline_value - base_date).days if deadline_value else None,
        priority_missing=priority_missing,
        priority=priority,
        matched=priority_tool.matched_keywords(
            _match_text(title, description), rules=rules
        ),
    )

    # ---------- 5. 按固定顺序组装输出 ----------
    return {
        "title": title,
        "department": _clean_text(task.get("department")),
        "owner": owner,
        "deadline": deadline_text,
        "priority": priority,
        "status": status,
        "description": description,
        "risk_level": risk_level,
        "need_confirm": need_confirm,
        "suggestions": suggestions,
    }


# ------------------------------------------------------------
# 内部工具函数
# ------------------------------------------------------------


def _build_suggestions(
    owner_missing: bool,
    deadline_missing: bool,
    overdue: bool,
    days_left: int | None,
    priority_missing: bool,
    priority: str,
    matched: list[tuple[str, str]],
) -> list[str]:
    """生成建议列表（每个风险点一条，顺序固定，便于界面展示）。"""
    suggestions: list[str] = []

    # 1. 负责人缺失
    if owner_missing:
        suggestions.append(MSG_OWNER_MISSING)

    # 2. 截止时间：缺失 / 已逾期 / 临近 三选一
    if deadline_missing:
        suggestions.append(MSG_DEADLINE_MISSING)
    elif overdue:
        suggestions.append(MSG_DEADLINE_OVERDUE)
    elif days_left is not None and days_left <= settings.REMINDER_DUE_SOON_DAYS:
        suggestions.append(MSG_DEADLINE_DUE_SOON)

    # 3. 优先级由规则推断而来
    if priority_missing:
        suggestions.append(MSG_PRIORITY_MISSING.format(level=priority))

    # 4. 命中高风险关键词（取第一个命中项，避免建议冗余）
    high_hits = [keyword for level, keyword in matched if level == "high"]
    if high_hits:
        suggestions.append(MSG_HIGH_RISK_KEYWORD.format(keyword=high_hits[0]))

    return suggestions


def _match_text(title: str, description: str | None) -> str:
    """拼接用于关键词匹配的文本：标题 + 描述。"""
    return f"{title} {description or ''}".strip()


def _clean_text(value: Any) -> str | None:
    """清洗文本：去首尾空白，空字符串按缺失（None）处理。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_priority(value: Any) -> str | None:
    """归一化优先级；缺失或非法返回 None（由关键词规则推断）。"""
    text = _clean_text(value)
    if text is None:
        return None
    lowered = text.lower()
    return lowered if lowered in settings.PRIORITIES else None


def _parse_deadline(value: Any) -> date | None:
    """解析截止时间为 date；支持 2026-09-30 / 2026/9/30 / 2026.9.30 / 2026年9月30日。

    注意：本函数只做格式解析，不做日期推断，因此「尽快」「下周三」视为缺失。
    """
    text = _clean_text(value)
    if text is None:
        return None

    matched = _DEADLINE_RE.search(text)
    if not matched:
        return None

    year, month, day = (int(part) for part in matched.groups())
    try:
        return date(year, month, day)
    except ValueError:  # 例如 2026-02-30
        return None
