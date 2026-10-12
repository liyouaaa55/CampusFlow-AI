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
    # --- 执行链路类（v2.1）---
    models.ACTION_DISPATCH: "系统内下发",
    models.ACTION_SUBMIT: "提交工作回执",
    # --- 结果核验类（v2.2）---
    models.ACTION_VERIFY: "结果核验",
    models.ACTION_NEED_SUPPLEMENT: "要求补充",
    models.ACTION_REJECT_REWORK: "驳回重办",
    models.ACTION_CLOSE: "任务闭环",
}


# ============================================================
# 四之二、负责人回执提交态（v2.1）
# ============================================================
#
# 为什么单独成一张表，而不并入上面的 STATUS_LABELS：
#   STATUS_LABELS 描述的是 **tasks.status**（任务自身状态），
#   而这三个词描述的是 **task_submissions 的事实**（负责人交没交回执）。
#   两者是两个独立维度：任务可以是「进行中」，同时回执是「待提交」。
#   混进同一张表会让「状态」这个概念在界面上产生歧义，
#   也会与 test_framework 锁定的 STATUS_LABELS 集合冲突。
#
# 英文键只存在于程序内部，界面上永不出现（沿用本模块「不泄漏英文」纪律）。
SUBMISSION_LABELS: dict[str, str] = {
    "pending_submit": "待提交",
    "submitted": "已提交",
    "overdue_unsubmitted": "已逾期未提交",
}


# ============================================================
# 四之三、核验状态（v2.2）
# ============================================================
#
# 为什么第三张状态表：
#   STATUS_LABELS      → tasks.status          任务自身状态
#   SUBMISSION_LABELS  → task_submissions 事实  负责人交没交
#   VERIFICATION_LABELS → verifications.status  交上来的东西算不算办结
#
# 「已提交」不等于「已闭环」，只有 approved 才代表真正办结。
# 三者混用会让「提交 = 完成 = 闭环」的错误语义重新出现。
VERIFICATION_LABELS: dict[str, str] = {
    models.VERIFICATION_PENDING: "待核验",
    models.VERIFICATION_APPROVED: "核验通过",
    models.VERIFICATION_NEED_SUPPLEMENT: "需补充",
    models.VERIFICATION_REJECTED: "驳回重办",
    models.VERIFICATION_MANUAL_REVIEW: "人工复核",
}

# 执行回执状态的展示顺序（与 SUBMISSION_LABELS 的键一一对应）
SUBMISSION_ORDER: tuple[str, ...] = (
    "pending_submit",
    "submitted",
    "overdue_unsubmitted",
)


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


def submission_label(state: Any) -> str:
    """回执提交态 → 中文（待提交 / 已提交 / 已逾期未提交）。

    与 status_label 分开：前者回答「任务本身处于什么状态」，
    本函数回答「负责人交没交回执」。查不到映射时同样返回占位符，
    绝不把 pending_submit 这类内部枚举泄漏到界面上。
    """
    key = str(state or "").strip()
    if not key:
        return UNKNOWN_TEXT
    return SUBMISSION_LABELS.get(key, UNKNOWN_TEXT)


# ============================================================
# 四之二、办结标准来源（v2.2）
# ============================================================
#
# 优先级由高到低：材料原文 > 静态规则 > AI 建议。
# AI 建议在数据层被强制为「未确认」，界面上必须让督办人员一眼看出
# 这条标准还没被确认，避免把 AI 猜测当成硬性办结要求。
CLOSURE_SOURCE_LABELS: dict[str, str] = {
    models.CLOSURE_SOURCE_MATERIAL: "材料明确",
    models.CLOSURE_SOURCE_RULE: "规则推导",
    models.CLOSURE_SOURCE_AI: "AI 建议",
    models.CLOSURE_SOURCE_MANUAL: "人工确认",
}

# 待确认状态的统一提示（AI 来源恒为未确认）
CLOSURE_UNCONFIRMED_HINT: str = "尚未经督办人员确认，不能作为强制办结标准。"

# ============================================================
# 核验四维度（verification_service 写入的四个布尔字段）
# ============================================================

VERIFICATION_DIMENSION_LABELS: dict[str, str] = {
    "requirement_match": "符合办结要求",
    "deliverable_complete": "交付物齐备",
    "process_valid": "办理过程合规",
    "state_consistent": "状态与材料一致",
}

# 核验四维度的固定展示顺序（与 verification_service 写入顺序一致）
VERIFICATION_DIMENSIONS: tuple[str, ...] = (
    "requirement_match",
    "deliverable_complete",
    "process_valid",
    "state_consistent",
)

# 闭环状态（closure_service.get_closure_status 返回）
CLOSURE_STATE_LABELS: dict[str, str] = {
    "ready": "具备闭环条件",
    "not_ready": "暂不具备闭环条件",
    "closed": "已闭环",
    "stale": "闭环依据已过期",
}


def verification_label(status: Any) -> str:
    """核验状态 → 中文。取不到映射时返回占位符，不向界面泄漏英文枚举。"""
    key = str(status or "").strip()
    return VERIFICATION_LABELS.get(key, UNKNOWN_TEXT)


def verification_dimension_label(field: Any) -> str:
    """核验维度字段名 → 中文。"""
    key = str(field or "").strip()
    return VERIFICATION_DIMENSION_LABELS.get(key, UNKNOWN_TEXT)


def closure_state_label(state: Any) -> str:
    """闭环状态 → 中文。"""
    key = str(state or "").strip()
    return CLOSURE_STATE_LABELS.get(key, UNKNOWN_TEXT)


def closure_source_label(source: Any) -> str:
    """办结标准来源 → 中文。"""
    key = str(source or "").strip()
    return CLOSURE_SOURCE_LABELS.get(key, UNKNOWN_TEXT)


# ============================================================
# 下发前智能分析结论（v3.0 第一阶段）
# ============================================================
#
# readiness 描述「这项任务现在能不能下发」，与提醒等级、任务状态都不同：
#   ready            逐项核对后未发现需要额外处理的问题
#   needs_supplement 缺少关键执行信息，需先补充
#   risky            存在明显风险或矛盾，需管理者确认
#   closed           已正式闭环，不再需要下发前分析
PREDISPATCH_READINESS_LABELS: dict[str, str] = {
    "ready": "具备下发条件",
    "needs_supplement": "需补充关键信息",
    "risky": "存在风险需确认",
    "closed": "已正式闭环",
}

# 问题严重度
ISSUE_SEVERITY_LABELS: dict[str, str] = {"high": "高", "medium": "中", "low": "低"}


def readiness_label(value: Any) -> str:
    """下发前分析结论 → 中文。"""
    key = str(value or "").strip()
    return PREDISPATCH_READINESS_LABELS.get(key, UNKNOWN_TEXT)


def severity_label(value: Any) -> str:
    """问题严重度 → 中文。"""
    key = str(value or "").strip()
    return ISSUE_SEVERITY_LABELS.get(key, UNKNOWN_TEXT)


# ============================================================
# 执行反馈状态（v3.0 第二阶段）
# ============================================================
#
# 与 tasks.status 是**两个维度**：
#   feedback_status 回答「管理者最近一次反馈说了什么」
#   status          回答「任务当前执行到什么状态」
#   "none" 是展示态（数据库中不存在该值，表示从未反馈）。
FEEDBACK_LABELS: dict[str, str] = {
    "none": "未反馈",
    models.FEEDBACK_COMPLETED: "已完成",
    models.FEEDBACK_IN_PROGRESS: "进行中",
}


def feedback_label(value: Any) -> str:
    """执行反馈状态 → 中文。"""
    key = str(value or "").strip()
    return FEEDBACK_LABELS.get(key, UNKNOWN_TEXT)