"""ReminderAgent：主动督办智能体（不调用 LLM）。

职责：主动扫描 Agent Memory（SQLite）中的任务，发现**临期 / 紧急 / 逾期**风险，
生成可直接推送或展示的提醒对象。它不是「查询数据库」，而是「发现风险」。

数据来源：默认 database.sqlite_db.list_tasks()，也支持传入任务列表（供前端复用缓存）。

等级判定（阈值全部读取 config/settings.py，不写死）：
    days < 0                          → overdue  已逾期
    0 <= days <= REMINDER_URGENT_DAYS (1)   → urgent   紧急（含当天截止）
    days <= REMINDER_DUE_SOON_DAYS    (3)   → due_soon 临期
    其他                              → normal   正常

字段与颜色等展示信息读取 knowledge/rules/reminder.json。

两条重要约定：
1. 已完成（status = completed）的任务默认不再督办；若显式传入 status 参数筛选，
   则以调用方的筛选为准。
2. 缺少截止时间（或日期非法）的任务无法判定等级，不作为提醒对象——
   这类任务的缺失问题由 PlannerAgent 的 risk_level / need_confirm 呈现。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import settings
from database import sqlite_db

# 提醒级别（顺序即输出与统计顺序，与 Dashboard 展示顺序一致）
REMINDER_LEVELS: tuple[str, ...] = ("normal", "due_soon", "urgent", "overdue")

# 级别严重程度排序（用于提醒列表排序：越靠前越紧急）
LEVEL_ORDER: dict[str, int] = {"overdue": 0, "urgent": 1, "due_soon": 2, "normal": 3}

# 默认不纳入督办的状态
EXCLUDED_STATUSES: tuple[str, ...] = ("completed",)

# 提醒对象字段（与需求文档一致，顺序固定）
REMINDER_FIELDS: tuple[str, ...] = (
    "task_id",
    "title",
    "level",
    "days_left",
    "department",
    "owner",
    "message",
)

# 等级定义文件路径
REMINDER_RULES_PATH: Path = settings.RULES_DIR / "reminder.json"

# 提醒文案模板（days 为剩余天数的绝对值；负值天数由 overdue 模板使用绝对值）
MESSAGE_TEMPLATES: dict[str, str] = {
    "overdue": "已逾期{days}天，请立即处理。",
    "urgent": "距离截止还有{days}天，请及时完成。",
    "due_soon": "距离截止还有{days}天，请提前安排。",
    "normal": "距离截止还有{days}天，请按计划推进。",
}

# 当天截止（days == 0）的专门文案，避免出现「还有 0 天」的别扭表述
MESSAGE_TODAY: str = "今日截止，请及时完成。"

# 等级定义缓存：{文件路径: {"normal": {...}, ...}}
_LEVEL_RULES_CACHE: dict[str, dict[str, dict[str, str]]] = {}


class ReminderRulesError(RuntimeError):
    """提醒等级定义文件缺失、格式非法或级别不全。"""


# ============================================================
# 一、等级定义（knowledge/rules/reminder.json）
# ============================================================


def load_level_rules(
    path: str | Path | None = None,
    force_reload: bool = False,
) -> dict[str, dict[str, str]]:
    """读取提醒等级定义（label / color / description）。

    异常：ReminderRulesError 文件缺失、JSON 非法、级别缺失或结构错误。
    """
    rules_path = Path(path) if path else REMINDER_RULES_PATH
    cache_key = str(rules_path)

    if not force_reload and cache_key in _LEVEL_RULES_CACHE:
        return _LEVEL_RULES_CACHE[cache_key]

    if not rules_path.exists():
        raise ReminderRulesError(f"提醒等级定义文件不存在：{rules_path}")

    try:
        raw = json.loads(rules_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReminderRulesError(f"提醒等级定义文件不是合法 JSON：{rules_path}（{exc}）") from exc

    levels = raw.get("levels") if isinstance(raw, dict) else None
    if not isinstance(levels, dict) or not levels:
        raise ReminderRulesError(f"提醒等级定义缺少 levels 字段：{rules_path}")

    parsed: dict[str, dict[str, str]] = {}
    for level, meta in levels.items():
        if not isinstance(meta, dict):
            raise ReminderRulesError(f"级别 {level} 的定义必须是对象：{rules_path}")
        parsed[level] = {
            "label": str(meta.get("label") or level),
            "color": str(meta.get("color") or ""),
            "description": str(meta.get("description") or ""),
        }

    # 四个级别必须齐备，避免出现无法展示的等级
    missing = [level for level in REMINDER_LEVELS if level not in parsed]
    if missing:
        raise ReminderRulesError(f"提醒等级定义缺少级别：{'/'.join(missing)}（{rules_path}）")

    _LEVEL_RULES_CACHE[cache_key] = parsed
    return parsed


def clear_rules_cache() -> None:
    """清空等级定义缓存。"""
    _LEVEL_RULES_CACHE.clear()


def level_label(level: str) -> str:
    """返回级别中文名（如 urgent → 紧急）；未知级别原样返回。"""
    return load_level_rules().get(level, {}).get("label", level)


def level_color(level: str) -> str:
    """返回级别对应的颜色（供 Dashboard 标记，如 red / orange）。"""
    return load_level_rules().get(level, {}).get("color", "")


# ============================================================
# 二、等级判定（纯函数，阈值取自 settings）
# ============================================================


def compute_level(days_left: int) -> str:
    """按剩余天数判定提醒级别。"""
    if days_left < 0:
        return "overdue"
    if days_left <= settings.REMINDER_URGENT_DAYS:
        return "urgent"
    if days_left <= settings.REMINDER_DUE_SOON_DAYS:
        return "due_soon"
    return "normal"


def compute_days_left(deadline: Any, today: date) -> int | None:
    """计算剩余天数（截止日 - 基准日）；日期缺失或非法返回 None。"""
    if deadline is None:
        return None
    text = str(deadline).strip()
    if not text:
        return None
    try:
        deadline_date = date.fromisoformat(text)
    except ValueError:
        return None
    return (deadline_date - today).days


def build_message(level: str, days_left: int) -> str:
    """生成基础提醒文案。"""
    if level == "urgent" and days_left == 0:
        return MESSAGE_TODAY
    template = MESSAGE_TEMPLATES.get(level, MESSAGE_TEMPLATES["normal"])
    return template.format(days=abs(days_left))


# ============================================================
# 三、对外接口
# ============================================================


def check_all(
    status: str | None = None,
    tasks: Sequence[dict[str, Any]] | None = None,
    today: date | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """扫描全部任务，返回提醒列表（按紧急程度排序）。

    参数：
        status   可选状态筛选，例如 "processing"；传入时该筛选优先于默认的
                 「已完成不督办」规则
        tasks    可直接传入任务列表（如前端缓存），默认从 SQLite 读取
        today    基准日期，默认今天，可注入以便复算或测试
        db_path  数据库路径，默认 settings.DB_PATH
    返回：
        提醒对象列表，字段见 REMINDER_FIELDS；无风险任务不返回（空列表）
    """
    return _scan(
        _load_tasks(status=status, tasks=tasks, db_path=db_path),
        today,
        # 未显式筛选状态时，默认跳过已完成任务
        skip_completed=status is None,
    )


def check_department(
    department: str,
    status: str | None = None,
    tasks: Sequence[dict[str, Any]] | None = None,
    today: date | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """按部门扫描，返回该部门的提醒列表（跨部门互不干扰）。"""
    return _scan(
        _load_tasks(status=status, department=department, tasks=tasks, db_path=db_path),
        today,
        skip_completed=status is None,
    )


def check_task(
    task_id: int,
    today: date | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """检查单个任务。

    返回：提醒对象；任务不存在、已完成、或缺少截止时间时返回 None。
    """
    task = sqlite_db.get_task(task_id, db_path=db_path)
    if task is None:
        return None
    if task.get("status") in EXCLUDED_STATUSES:
        return None
    return _to_reminder(task, today or date.today())


def count_levels(
    items: Sequence[dict[str, Any]] | None = None,
    status: str | None = None,
    department: str | None = None,
    tasks: Sequence[dict[str, Any]] | None = None,
    today: date | None = None,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """统计各提醒级别的数量，供 Dashboard 使用。

    返回固定四个键（无数据时为 0）：
        {"normal": 12, "due_soon": 3, "urgent": 2, "overdue": 1}
    """
    if items is None:
        items = _scan(
            _load_tasks(status=status, department=department, tasks=tasks, db_path=db_path),
            today,
            skip_completed=status is None,
        )

    counts = {level: 0 for level in REMINDER_LEVELS}
    for item in items:
        level = item.get("level")
        if level in counts:
            counts[level] += 1
    return counts


def generate_messages(
    items: Sequence[dict[str, Any]] | None = None,
    status: str | None = None,
    department: str | None = None,
    tasks: Sequence[dict[str, Any]] | None = None,
    today: date | None = None,
    db_path: str | Path | None = None,
) -> list[str]:
    """生成面向负责人的提醒文案列表（带级别标签，可直接推送或导出）。

    未传入 items 时自动执行一次扫描。
    """
    if items is None:
        items = _scan(
            _load_tasks(status=status, department=department, tasks=tasks, db_path=db_path),
            today,
            skip_completed=status is None,
        )
    return [format_reminder_message(item) for item in items]


def format_reminder_message(reminder: dict[str, Any]) -> str:
    """把单条提醒对象格式化为一行推送文案。"""
    label = level_label(reminder.get("level", ""))
    title = reminder.get("title") or "未命名任务"
    owner = reminder.get("owner") or "未指定"
    department = reminder.get("department") or "未指定"
    message = reminder.get("message") or ""
    return f"【{label}】{title}：{message}（负责人：{owner}；部门：{department}）"


# ============================================================
# 四、内部实现
# ============================================================


def _load_tasks(
    status: str | None = None,
    department: str | None = None,
    tasks: Sequence[dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """获取待扫描的任务集合：优先使用传入列表，否则读取数据库。"""
    if tasks is None:
        return sqlite_db.list_tasks(status=status, department=department, db_path=db_path)

    if not isinstance(tasks, (list, tuple)):
        raise TypeError(f"tasks 需要列表，收到：{type(tasks).__name__}")

    selected: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise TypeError(f"任务必须是字典，收到：{type(task).__name__}")
        if status is not None and task.get("status") != status:
            continue
        if department is not None and task.get("department") != department:
            continue
        selected.append(task)
    return selected


def _scan(
    tasks: Iterable[dict[str, Any]],
    today: date | None,
    skip_completed: bool = True,
) -> list[dict[str, Any]]:
    """对任务集合做风险扫描，返回排序后的提醒列表。

    skip_completed 为 True 时跳过已完成任务（默认行为）；
    调用方显式传入 status 筛选时传 False，表示以调用方的筛选为准。
    """
    base_date = today or date.today()

    reminders: list[dict[str, Any]] = []
    for task in tasks:
        if skip_completed and task.get("status") in EXCLUDED_STATUSES:
            continue
        reminder = _to_reminder(task, base_date)
        if reminder is not None:
            reminders.append(reminder)

    return _sort_reminders(reminders)


def _to_reminder(task: dict[str, Any], today: date) -> dict[str, Any] | None:
    """把一条任务转换为提醒对象；无法判定等级时返回 None。"""
    days_left = compute_days_left(task.get("deadline"), today)
    if days_left is None:
        return None

    level = compute_level(days_left)
    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "level": level,
        "days_left": days_left,
        "department": task.get("department"),
        "owner": task.get("owner"),
        "message": build_message(level, days_left),
    }


def _sort_reminders(reminders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """排序：先按严重程度（逾期 → 紧急 → 临期 → 正常），再按剩余天数升序，最后按任务号。"""
    return sorted(
        reminders,
        key=lambda item: (
            LEVEL_ORDER.get(item["level"], len(LEVEL_ORDER)),
            item["days_left"],
            item.get("task_id") or 0,
        ),
    )
