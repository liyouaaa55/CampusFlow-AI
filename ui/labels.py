"""展示层中文标签映射：内部英文枚举 → 面向用户的中文。

为什么需要这个模块
------------------
项目内部一律使用稳定的英文枚举（normal / due_soon / green / analyze / ...），
它们写进数据库、进入测试断言、跨模块传递，**一个字都不能改**。
但用户看到的界面、报告、日志里绝不能出现英文。

本模块就是那层「只进不改」的转换：

    内部枚举（database / agent 层）  →  本模块  →  中文界面（ui / 报告）

三条纪律
--------
1. **不改内部枚举。** 本模块只做「查表换字」，不参与任何业务判断。
2. **单一来源。** 提醒等级以 knowledge/rules/reminder.json 的 label 为准
   （由 reminder_agent.level_label() 读取），本模块只做转发，不另存一份，
   避免出现两处定义互相漂移。
3. **不泄漏英文。** 取不到映射时返回占位符「—」而**不是原始值**，
   这样新增枚举若忘记补中文，页面显示为「—」而不是漏出英文。
   tests/test_ui_labels.py 会断言「所有内部枚举都有中文标签」，
   漏配会直接测试失败。
"""

from __future__ import annotations

from typing import Any

from agent import reminder_agent
from config import settings
from database import models

# 未评估 / 未映射时的占位符（与 ui/components/tables.py 的既有口径一致）
UNKNOWN_TEXT: str = "—"


# ============================================================
# 一、任务状态与优先级（直接复用全局配置，避免两处定义）
# ============================================================

STATUS_LABELS: dict[str, str] = dict(settings.STATUS_LABELS)
PRIORITY_LABELS: dict[str, str] = dict(settings.PRIORITY_LABELS)

# 界面口径微调：urgent 是「由截止时间推导的临期提醒」，
# 与提醒等级里的「临期」用同一个词更符合业务人员的说法。
STATUS_LABELS_DISPLAY: dict[str, str] = {**STATUS_LABELS, "urgent": "临期"}

# 枚举的展示顺序（与全局配置严格一致）。
# 下拉选项必须按此顺序构建，因此顺序常量与标签常量放在同一模块，
# 让显示层不依赖 config，避免两处各取一半导致顺序与标签错位。
STATUS_ORDER: tuple[str, ...] = tuple(settings.TASK_STATUSES)
PRIORITY_ORDER: tuple[str, ...] = tuple(settings.PRIORITIES)


# ============================================================
# 二、风险等级（green / yellow / red）
# ============================================================

RISK_LABELS: dict[str, str] = {
    "green": "低风险",
    "yellow": "中风险",
    "red": "高风险",
}


# ============================================================
# 三、日志动作（新建 + 状态变更 + 事务事件）
# ============================================================

LOG_ACTION_LABELS: dict[str, str] = {
    # --- 状态类 ---
    models.ACTION_CREATE: "创建",
    "pending": "标记为待办",
    "confirmed": "标记为已确认",
    "processing": "标记为进行中",
    "urgent": "标记为临期",
    "completed": "标记为已完成",
    "overdue": "标记为已逾期",
    # --- 事务事件类（v2.0）---
    models.ACTION_ANALYZE: "智能分析",
    models.ACTION_DRAFT: "生成提醒草稿",
    models.ACTION_REQUEST: "请求补充信息",
    models.ACTION_CONFIRM: "人工确认",
    models.ACTION_REJECT: "人工否决",
    models.ACTION_ESCALATE: "提级督办",
}


# ============================================================
# 四、处置动作（execution_tool 的三个受控动作）
# ============================================================

# 业务动作名（DRAFT_NOTIFY / REQUEST_INFO / ESCALATE）→ 面向用户的中文。
# 键是 tools.execution_tool 定义的业务动作，与日志事件 escalate 语义一致，
# 中文名刻意相同：用户看到「提级督办」，日志里也记「提级督办」。
ACTION_LABELS: dict[str, str] = {
    "DRAFT_NOTIFY": "生成提醒草稿",
    "REQUEST_INFO": "请求补充信息",
    "ESCALATE": "提级督办",
}


# ============================================================
# 五、任务字段名（用于「请求补充信息」列出待补字段）
# ============================================================

FIELD_LABELS: dict[str, str] = {
    "owner": "负责人",
    "department": "责任部门",
    "deadline": "截止时间",
    "title": "任务标题",
    "priority": "优先级",
    "description": "事项要求",
}


# ============================================================
# 六、映射函数
# ============================================================


def action_label(action: Any) -> str:
    """处置动作 → 中文。

    同时接受业务动作名（DRAFT_NOTIFY）与日志事件名（escalate），
    两者中文名一致，页面无需区分是哪一套命名。
    """
    key = str(action or "").strip()
    if not key:
        return UNKNOWN_TEXT
    if key in ACTION_LABELS:
        return ACTION_LABELS[key]
    if key in LOG_ACTION_LABELS:
        return LOG_ACTION_LABELS[key]
    return UNKNOWN_TEXT


def field_label(field: Any) -> str:
    """任务字段名（owner / deadline …）→ 中文。"""
    key = str(field or "").strip()
    if not key:
        return UNKNOWN_TEXT
    return FIELD_LABELS.get(key, UNKNOWN_TEXT)


def level_label(level: Any) -> str:
    """提醒等级（normal / due_soon / urgent / overdue）→ 中文。

    权威来源为 knowledge/rules/reminder.json，由 reminder_agent.level_label()
    读取，本模块不另存一份。查不到时返回占位符，不把英文漏给用户。
    """
    try:
        label = reminder_agent.level_label(str(level or ""))
    except reminder_agent.ReminderRulesError:
        return UNKNOWN_TEXT
    # reminder_agent 对未知等级会原样返回英文，这里兜底成占位符
    if not label or label == level:
        return UNKNOWN_TEXT
    return label


def level_color(level: Any) -> str:
    """提醒等级 → 展示色（green / yellow / orange / red）。"""
    try:
        return reminder_agent.level_color(str(level or ""))
    except reminder_agent.ReminderRulesError:
        return ""


def status_label(status: Any) -> str:
    """任务状态 → 中文（界面口径，urgent 显示为「临期」）。"""
    if not status:
        return UNKNOWN_TEXT
    return STATUS_LABELS_DISPLAY.get(str(status), UNKNOWN_TEXT)


def priority_label(priority: Any) -> str:
    """优先级 → 中文（高 / 中 / 低）。"""
    if not priority:
        return UNKNOWN_TEXT
    return PRIORITY_LABELS.get(str(priority), UNKNOWN_TEXT)


def risk_label(risk: Any) -> str:
    """风险等级（green / yellow / red）→ 中文（低风险 / 中风险 / 高风险）。"""
    key = str(risk or "").strip()
    if not key:
        return UNKNOWN_TEXT
    return RISK_LABELS.get(key, UNKNOWN_TEXT)


def log_action_label(action: Any) -> str:
    """日志动作 → 中文。取不到映射时返回占位符，绝不把英文漏给用户。"""
    key = str(action or "").strip()
    if not key:
        return UNKNOWN_TEXT
    return LOG_ACTION_LABELS.get(key, UNKNOWN_TEXT)
