"""执行处置工具：受控动作的规则选择、合法性校验与最小执行（v2.0）。

定位：**纯业务 / 工具层**。本模块不知道 UI、session_state 与大模型的存在，
因此可以被 ExecutionAgent、页面或脚本复用，也便于纯函数单测。

核心职责（严格四件事）：
1. 定义 v2.0 允许的受控动作，以及永远不允许执行的黑名单动作；
2. select_action()  —— 依据确定性业务规则选定动作（纯函数，不碰数据库）；
3. validate_plan()  —— 七层校验，任何不合法的计划都被挡在执行之前；
4. build_remark() / execute() —— 构造可审计备注、执行最小动作。

三条不可让步的设计约束：

- **动作由规则决定，不由模型决定。** select_action() 是纯函数；
  normalize_plan() 会丢弃外部传入的 action / params 并用规则结果覆盖。
  大模型只能提供 situation / reasons 这类「解释性文本」。
- **状态与风险等级是两个独立维度。** status 中的 urgent / overdue 是
  「由 deadline 推导的时间层级」（见 settings.TASK_STATUSES 注释），
  risk_level 是 green / yellow / red 风险等级。ESCALATE 只提 risk_level，
  绝不修改 status。
- **黑名单优先于允许集合。** 即使某个黑名单动作未来被误加进 ALLOWED_ACTIONS，
  校验与执行两层仍会独立拒绝它。

复用既有能力，不重复实现：
- 剩余天数与提醒等级一律调用 reminder_agent.compute_days_left() /
  compute_level()，本模块不建立第二套 deadline 判断规则；
- 枚举、默认操作人、风险等级取值一律取自 database.models。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent import reminder_agent
from database import models, sqlite_db

# ============================================================
# 一、动作常量
# ============================================================

# --- 1.1 允许的受控动作（v2.0 第一阶段仅三个）---

ACTION_DRAFT_NOTIFY: str = "DRAFT_NOTIFY"    # 生成提醒 / 督办文案草稿（不发送）
ACTION_REQUEST_INFO: str = "REQUEST_INFO"    # 请求人工补充缺失信息（不自动改字段）
ACTION_ESCALATE: str = "ESCALATE"            # 提级督办：risk_level -> red

ALLOWED_ACTIONS: tuple[str, ...] = (
    ACTION_DRAFT_NOTIFY,
    ACTION_REQUEST_INFO,
    ACTION_ESCALATE,
)

# --- 1.2 黑名单动作（任何情况下都不允许执行）---

BLACKLIST_MARK_COMPLETED: str = "MARK_COMPLETED"      # 禁止把任务标记为已完成
BLACKLIST_MODIFY_OWNER: str = "MODIFY_OWNER"          # 禁止自动改负责人
BLACKLIST_MODIFY_DEADLINE: str = "MODIFY_DEADLINE"    # 禁止自动改截止时间
BLACKLIST_MODIFY_TITLE: str = "MODIFY_TITLE"          # 禁止自动改标题
BLACKLIST_MODIFY_STATUS: str = "MODIFY_STATUS"        # 禁止自动改任务状态
BLACKLIST_SEND_EXTERNAL: str = "SEND_EXTERNAL"        # 禁止自动发送任何外部消息

BLACKLISTED_ACTIONS: frozenset[str] = frozenset(
    {
        BLACKLIST_MARK_COMPLETED,
        BLACKLIST_MODIFY_OWNER,
        BLACKLIST_MODIFY_DEADLINE,
        BLACKLIST_MODIFY_TITLE,
        BLACKLIST_MODIFY_STATUS,
        BLACKLIST_SEND_EXTERNAL,
    }
)

# --- 1.3 动作 -> 任务日志事件的映射（用于去重判定）---

# 业务动作与 task_logs.action 是两个命名体系：
#   业务动作 DRAFT_NOTIFY  <->  日志事件 "draft"（models.ACTION_DRAFT）
# 映射集中在此，避免散落各处的字符串字面量。
ACTION_LOG_EVENT: dict[str, str] = {
    ACTION_DRAFT_NOTIFY: models.ACTION_DRAFT,
    ACTION_REQUEST_INFO: models.ACTION_REQUEST,
    ACTION_ESCALATE: models.ACTION_ESCALATE,
}

# 参与「重复处置」判定的日志事件：这三个代表「动作真的执行过」。
DEDUP_EVENTS: frozenset[str] = frozenset(ACTION_LOG_EVENT.values())

# 明确不参与去重的日志事件：
#   analyze —— 只是「分析过一次」，不代表执行过任何动作，因此允许重新分析；
#   confirm —— 只是「人工确认过」，真正的动作仍会各自留痕（draft / escalate）；
#   reject  —— 人工否决后必须允许再次提出方案，否则任务将被永久锁死。
NON_DEDUP_EVENTS: frozenset[str] = frozenset(
    {models.ACTION_ANALYZE, models.ACTION_CONFIRM, models.ACTION_REJECT}
)

# 重复处置的判定窗口（天）。
# 项目现有 settings 中只有提醒阈值（URGENT / DUE_SOON / NORMAL_DAYS），
# 语义都是「提醒紧迫度」而非「处置去重」，复用会造成语义混淆，
# 因此在此单独定义一个语义清晰的小常量，不新增复杂配置。
ACTION_DEDUP_DAYS: int = 3

# ============================================================
# 二、参数与阈值常量
# ============================================================

# ESCALATE 唯一允许的目标风险等级。
# 必须是 red：只有「已逾期且尚未提级」才需要提级。
ESCALATE_TARGET_RISK: str = "red"

# 允许生成提醒草稿的提醒等级（normal 不需要督办，故排除）。
DRAFT_NOTIFY_LEVELS: tuple[str, ...] = ("due_soon", "urgent", "overdue")

# params 中绝对不允许出现的字段——即便调用方（未来可能是大模型）提出，
# Validator 也必须拒绝。这些字段只能由人工在页面上修改。
FORBIDDEN_PARAM_FIELDS: tuple[str, ...] = (
    "owner",
    "department",
    "deadline",
    "title",
    "status",
)

# 缺失值在 remark 中的占位符（与 tools/report_tool.py 的口径一致）。
PLACEHOLDER: str = "—"

# 缺失的剩余天数在 remark 中的占位符。
NA: str = "NA"

# task_logs.remark 的最大长度。
# remark 是 TEXT 列但会被日志表格直接渲染，因此必须限长；
# 完整的结构化分析结果由调用方保留，不塞进数据库。
REMARK_MAX_LENGTH: int = 180

# 已完成状态：不再纳入任何处置。
COMPLETED_STATUS: str = "completed"

# 默认风险说明（按动作区分，用于界面提示与审计）。
RISK_NOTES: dict[str, str] = {
    ACTION_DRAFT_NOTIFY: "仅生成提醒草稿供人工参考，系统不会自动发送任何外部消息。",
    ACTION_REQUEST_INFO: "仅提示人工补充信息，系统不会自动修改任务字段。",
    ACTION_ESCALATE: (
        "人工确认后把风险等级提升为 red（不修改任务状态），并写入 escalate 日志。"
    ),
    "": "当前无需处置。",
}


# ============================================================
# 三、结构化验证结果
# ============================================================


@dataclass(frozen=True)
class ValidationResult:
    """动作计划的校验结果。

    用结构化返回而不是抛异常，表达「正常的业务拒绝」：
    非法动作、已处置过、需人工确认等都属于预期结果，不应污染调用栈。
    仅当调用方违反函数契约（类型错误等）时才抛异常。
    """

    ok: bool
    reason: str
    action: str | None = None


def _ok(action: str, reason: str = "校验通过。") -> ValidationResult:
    """构造通过结果。"""
    return ValidationResult(ok=True, reason=reason, action=action)


def _reject(reason: str, action: str | None = None) -> ValidationResult:
    """构造拒绝结果。"""
    return ValidationResult(ok=False, reason=reason, action=action)


# ============================================================
# 四、纯工具函数
# ============================================================


def _clean_text(value: Any) -> str | None:
    """清洗文本：去首尾空白，空字符串按缺失（None）处理。

    与 planner_agent / sqlite_db 的同名私有函数口径一致，
    但此处保持独立，避免工具层反向依赖业务层的私有实现。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _token(value: Any, placeholder: str = PLACEHOLDER) -> str:
    """把任意值转成适合写入 remark 的短字符串（None / 空串走占位符）。"""
    if value is None:
        return placeholder
    if isinstance(value, str):
        return value.strip() or placeholder
    return str(value)


def build_params(action: str | None) -> dict[str, Any]:
    """按动作生成规范 params（唯一的参数来源，外部不得自定义）。"""
    if action == ACTION_ESCALATE:
        return {"risk_level": ESCALATE_TARGET_RISK}
    return {}


def build_remark(
    action: str | None,
    level: str | None = None,
    days_left: int | None = None,
    situation: str = "",
    max_length: int = REMARK_MAX_LENGTH,
) -> str:
    """构造可审计的任务日志备注。

    格式：``[{action}] level={level} days={days_left} | {situation}``

    设计取舍：
    - 只写「一行可读摘要」，不把完整 JSON 塞进 task_logs；
    - 长度上限 REMARK_MAX_LENGTH，超出即截断并加省略号；
    - 所有字段都经过字符串化清洗，None / 空值不会让函数崩溃。
    """
    if max_length <= 0:
        return ""

    head = (
        f"[{_token(action)}] level={_token(level)} days={_token(days_left, NA)}"
    )
    tail = _clean_text(situation)
    text = f"{head} | {tail}" if tail else head

    if len(text) <= max_length:
        return text
    return text[: max(1, max_length - 1)] + "…"


def resolve_level(
    task: Mapping[str, Any],
    today: date | None = None,
) -> tuple[str, int | None]:
    """由任务与基准日推出（提醒等级, 剩余天数）。

    直接复用 reminder_agent 的纯函数，不另建 deadline 判断规则。
    deadline 缺失或格式非法时返回 ("", None)——这类任务无法判定等级，
    其字段缺失问题由 PlannerAgent 的 need_confirm / risk_level 呈现。
    """
    base_date = today or date.today()
    days_left = reminder_agent.compute_days_left(task.get("deadline"), base_date)
    if days_left is None:
        return "", None
    return reminder_agent.compute_level(days_left), days_left


# ============================================================
# 五、select_action：确定性动作选择（纯函数）
# ============================================================


def select_action(
    level: str | None,
    task: Mapping[str, Any],
    logs: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    """按确定性业务规则选定下一步动作。

    参数：
        level  reminder_agent.compute_level() 的结果；
               为 None / 空 / 未知取值时按「无需处置」处理。
        task   单条任务记录（sqlite_db.get_task 的返回结构）。
        logs  该任务的历史日志。**本函数不参与去重判定**，
               重复处置统一由 validate_plan() 拦截，此处保留参数
               只是为了让调用方接口对称、便于后续扩展。

    返回：动作名；无需处置时返回 None。

    决策顺序（先命中先返回）：
        1. 任务已完成                          -> None
        2. 负责人或截止时间缺失                -> REQUEST_INFO
        3. 已逾期且尚未提级                    -> ESCALATE
        4. 已逾期但已提级                      -> DRAFT_NOTIFY（降级，仍产出可用文案）
        5. 临期 / 紧急                         -> DRAFT_NOTIFY
        6. 其他                                -> None

    第 4 条的必要性：若已提级就不能重复提级，但督办文案仍然有用，
    降级为 DRAFT_NOTIFY 可以继续产生实际可用的提醒，而不是什么都不做。
    """
    if not isinstance(task, Mapping):
        raise TypeError(f"任务必须是映射类型，收到：{type(task).__name__}")

    # 1. 已完成任务不再处置
    if _clean_text(task.get("status")) == COMPLETED_STATUS:
        return None

    # 2. 关键字段缺失时，优先请人工补全（补全之前谈不上督办）
    if _clean_text(task.get("owner")) is None or _clean_text(task.get("deadline")) is None:
        return ACTION_REQUEST_INFO

    normalized_level = _clean_text(level) or ""

    # 3 / 4. 已逾期：未提级则提级，已提级则降级为生成提醒草稿
    if normalized_level == "overdue":
        if _clean_text(task.get("risk_level")) != ESCALATE_TARGET_RISK:
            return ACTION_ESCALATE
        return ACTION_DRAFT_NOTIFY

    # 5. 临期 / 紧急：生成提醒草稿
    if normalized_level in ("due_soon", "urgent"):
        return ACTION_DRAFT_NOTIFY

    # 6. 正常（normal）或未知等级：无需处置
    return None


# ============================================================
# 六、normalize_plan：把外部输入收敛为可信计划
# ============================================================


def normalize_plan(
    task: Mapping[str, Any],
    level: str | None,
    *,
    raw: Mapping[str, Any] | None = None,
    reasons: Sequence[str] = (),
    risk_note: str = "",
) -> dict[str, Any]:
    """生成规范化的处置计划。

    **绝对不信任外部传入的 action / params。**
    ``raw`` 仅用于提取大模型产出的 situation 与 reasons 这类解释性文本；
    真正的动作与参数一律由 select_action() / build_params() 重新注入。
    因此即使 ``raw`` 是 ``{"action": "MARK_COMPLETED", "params": {"status": "completed"}}``，
    产出的计划里 action 也只可能是三个允许动作之一，或空字符串（无需处置）。

    这样设计的原因是：让「谁决定了要做什么」永远可追溯到规则，
    而不是依赖任何外部输入的自觉。
    """
    if not isinstance(task, Mapping):
        raise TypeError(f"任务必须是映射类型，收到：{type(task).__name__}")

    action = select_action(level, task)
    params = build_params(action)

    raw_situation = ""
    if isinstance(raw, Mapping):
        raw_situation = _clean_text(raw.get("situation")) or ""

    clean_reasons: list[str] = []
    for item in reasons:
        text = _clean_text(item)
        if text:
            clean_reasons.append(text)
    if not clean_reasons and isinstance(raw, Mapping):
        for item in raw.get("reasons") or ():
            text = _clean_text(item)
            if text:
                clean_reasons.append(text)

    return {
        "task_id": task.get("id"),
        "action": action or "",
        "params": params,
        # 第一阶段所有处置一律需要人工确认，不提供「自动执行」开关。
        "need_confirm": True,
        "level": _clean_text(level) or "",
        "situation": raw_situation,
        "reasons": clean_reasons,
        "risk_note": _clean_text(risk_note) or RISK_NOTES.get(action or "", RISK_NOTES[""]),
    }


# ============================================================
# 七、validate_plan：七层校验
# ============================================================


def validate_plan(
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
    logs: Sequence[Mapping[str, Any]] = (),
    *,
    today: date | None = None,
) -> ValidationResult:
    """校验处置计划是否允许执行。

    严格按 L2 -> L1 -> L3 -> L4 -> L5 -> L6 -> L7 的顺序执行，
    其中**黑名单（L2）优先于枚举校验（L1）**：即便黑名单动作被误加进
    ALLOWED_ACTIONS，本层仍会独立拒绝它。

    层级说明：
        L2 黑名单    —— 立即拒绝，永不进入后续判断
        L1 枚举      —— 动作必须属于 ALLOWED_ACTIONS
        L3 参数      —— 各动作的 params 形状必须精确匹配
        L4 前置条件  —— 依据 level / 字段缺失 / 风险等级判断动作是否成立
        L5 禁止字段  —— params 中不得出现受保护字段（纵深防御）
        L6 重复处置  —— 近期已有同动作事件则拒绝
        L7 人工确认  —— need_confirm 必须严格为 True

    返回：ValidationResult。**不抛异常**表达业务拒绝。
    """
    if not isinstance(task, Mapping):
        raise TypeError(f"任务必须是映射类型，收到：{type(task).__name__}")
    if not isinstance(plan, Mapping):
        raise TypeError(f"处置计划必须是映射类型，收到：{type(plan).__name__}")

    action = plan.get("action")
    action_text = _clean_text(action)
    params = plan.get("params")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        return _reject(
            f"params 必须是映射类型，收到：{type(params).__name__}", action_text
        )

    # ---------- L2 黑名单（优先于枚举校验）----------
    if action_text is not None and action_text in BLACKLISTED_ACTIONS:
        return _reject(f"动作 {action_text} 属于黑名单，任何情况下都不允许执行。", action_text)

    # ---------- L1 枚举 ----------
    if action_text not in ALLOWED_ACTIONS:
        return _reject(
            f"动作不在允许集合内：{_token(action_text)}"
            f"（允许值：{'/'.join(ALLOWED_ACTIONS)}）",
            action_text,
        )

    # ---------- L3 参数 ----------
    param_error = _validate_params(action_text, params)
    if param_error is not None:
        return _reject(param_error, action_text)

    # ---------- L4 前置条件 ----------
    precondition_error = _validate_preconditions(action_text, task, today)
    if precondition_error is not None:
        return _reject(precondition_error, action_text)

    # ---------- L5 禁止字段（纵深防御）----------
    forbidden = [field for field in FORBIDDEN_PARAM_FIELDS if field in params]
    if forbidden:
        return _reject(
            f"params 含有禁止修改的字段：{'/'.join(forbidden)}"
            "（此类字段只能由人工在页面修改）。",
            action_text,
        )

    # ---------- L6 重复处置 ----------
    dedup_error = _validate_dedup(action_text, logs, today)
    if dedup_error is not None:
        return _reject(dedup_error, action_text)

    # ---------- L7 人工确认 ----------
    if plan.get("need_confirm") is not True:
        return _reject(
            "need_confirm 必须严格为 True：v2.0 第一阶段所有处置均需人工确认。",
            action_text,
        )

    return _ok(action_text)


def _validate_params(action: str, params: Mapping[str, Any]) -> str | None:
    """L3：校验各动作的 params 形状。返回错误说明；合法则返回 None。"""
    if action in (ACTION_DRAFT_NOTIFY, ACTION_REQUEST_INFO):
        if params:
            return f"{action} 不接受任何参数，但收到：{sorted(params)}。"
        return None

    if action == ACTION_ESCALATE:
        expected = {"risk_level": ESCALATE_TARGET_RISK}
        if dict(params) != expected:
            return (
                f"{action} 的 params 必须严格为 {expected}，收到：{dict(params)}。"
                f"注意：urgent / overdue 是时间层级，不是风险等级，"
                f"风险等级只能是 {'/'.join(models.RISK_LEVELS)}。"
            )
        return None

    # 理论上不可达：动作枚举已在 L1 校验过
    return f"未识别的动作：{action}。"


def _validate_preconditions(
    action: str,
    task: Mapping[str, Any],
    today: date | None,
) -> str | None:
    """L4：校验动作的业务前置条件。返回错误说明；合法则返回 None。"""
    if _clean_text(task.get("status")) == COMPLETED_STATUS:
        return "任务已完成，不再纳入处置。"

    # level 一律由任务自身重新计算，绝不信任 plan 中可能存在的 level 字段
    level, _days_left = resolve_level(task, today)

    if action == ACTION_DRAFT_NOTIFY:
        if level not in DRAFT_NOTIFY_LEVELS:
            return (
                f"当前等级 {level or PLACEHOLDER} 不需要督办提醒"
                f"（仅 {'/'.join(DRAFT_NOTIFY_LEVELS)} 触发）。"
            )
        return None

    if action == ACTION_REQUEST_INFO:
        missing = [
            field
            for field in ("owner", "deadline")
            if _clean_text(task.get(field)) is None
        ]
        if not missing:
            return "负责人与截止时间均齐备，无需请求补充信息。"
        return None

    if action == ACTION_ESCALATE:
        if level != "overdue":
            return (
                f"只有已逾期（overdue）才需要提级，当前等级为 "
                f"{level or PLACEHOLDER}。"
            )
        if _clean_text(task.get("risk_level")) == ESCALATE_TARGET_RISK:
            return f"风险等级已是 {ESCALATE_TARGET_RISK}，无需重复提级。"
        return None

    return f"未识别的动作：{action}。"


def _validate_dedup(
    action: str,
    logs: Sequence[Mapping[str, Any]],
    today: date | None,
) -> str | None:
    """L6：重复处置判定。返回错误说明；允许处置则返回 None。

    只有 DEDUP_EVENTS（draft / request / escalate）代表「动作真的执行过」。
    analyze / confirm / reject 不构成重复依据——被否决后必须允许再次提出方案。
    """
    event = ACTION_LOG_EVENT.get(action)
    if event is None:
        return None

    base_date = today or date.today()
    # 统一成 datetime 再比较：把 cutoff 视为当天 00:00:00，语义更明确
    cutoff = datetime.combine(base_date, datetime.min.time()) - timedelta(
        days=ACTION_DEDUP_DAYS
    )

    for entry in logs or ():
        if not isinstance(entry, Mapping):
            continue
        if _clean_text(entry.get("action")) != event:
            continue
        stamp = _parse_log_time(entry.get("created_at"))
        if stamp is None:
            # 时间戳无法解析时无法判断是否落在窗口内，保守跳过。
            # 重复执行 DRAFT_NOTIFY 最多多生成一份草稿；
            # 重复执行 ESCALATE 仍会被 L4 的「已是 red」拦下，因此风险可控。
            continue
        if stamp >= cutoff:
            return (
                f"最近 {ACTION_DEDUP_DAYS} 天内已执行过 {event}"
                f"（{stamp.isoformat(sep=' ')}），不重复处置。"
            )
    return None


def _parse_log_time(value: Any) -> datetime | None:
    """解析 task_logs.created_at（形如 2026-09-23 10:30:00）；失败返回 None。"""
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ============================================================
# 八、execute：最小执行路径
# ============================================================


def execute(
    action: str,
    *,
    task: Mapping[str, Any],
    params: Mapping[str, Any] | None = None,
    operator: str = models.DEFAULT_OPERATOR,
    remark: str = "",
    today: date | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """执行受控动作（v2.0 最小实现）。

    各动作的实际能力边界：
        DRAFT_NOTIFY  —— 只生成提醒草稿文本并返回，**不发送任何外部消息**，
                         也**不写数据库**（draft 日志由调用方的处置流程写入）。
        REQUEST_INFO  —— 只返回「需要人工补充哪些字段」，**不修改任何数据库字段**。
        ESCALATE      —— 调用 sqlite_db.update_risk_level() 把 risk_level 提为 red，
                         由该函数在同一事务内写入 escalate 日志。
                         **绝不修改 status**，绝不直接执行 SQL。

    返回：``{"action", "applied", "task_id", "level", "days_left", "message", "task"}``
        其中 ``applied`` 表示本次是否真的改动了数据库。

    异常：ValueError 动作不在允许集合内、或属于黑名单、或 params 不合法。
        （execute 是最后一道防线，收到非法动作必须显式失败而不是静默返回。）
    """
    if not isinstance(task, Mapping):
        raise TypeError(f"任务必须是映射类型，收到：{type(task).__name__}")

    action_text = _clean_text(action)
    if action_text is not None and action_text in BLACKLISTED_ACTIONS:
        raise ValueError(f"动作 {action_text} 属于黑名单，拒绝执行。")
    if action_text not in ALLOWED_ACTIONS:
        raise ValueError(
            f"动作不在允许集合内：{_token(action_text)}"
            f"（允许值：{'/'.join(ALLOWED_ACTIONS)}）"
        )

    effective_params: Mapping[str, Any] = params if params is not None else {}
    if not isinstance(effective_params, Mapping):
        raise ValueError(f"params 必须是映射类型，收到：{type(effective_params).__name__}")
    param_error = _validate_params(action_text, effective_params)
    if param_error is not None:
        raise ValueError(param_error)

    task_id = task.get("id")
    level, days_left = resolve_level(task, today)

    if action_text == ACTION_DRAFT_NOTIFY:
        message = (
            reminder_agent.build_message(level, days_left)
            if days_left is not None
            else "缺少截止时间，无法生成提醒文案。"
        )
        return {
            "action": ACTION_DRAFT_NOTIFY,
            "applied": False,
            "task_id": task_id,
            "level": level,
            "days_left": days_left,
            "message": message,
            "task": None,
        }

    if action_text == ACTION_REQUEST_INFO:
        missing = [
            field
            for field in ("owner", "deadline")
            if _clean_text(task.get(field)) is None
        ]
        return {
            "action": ACTION_REQUEST_INFO,
            "applied": False,
            "task_id": task_id,
            "level": level,
            "days_left": days_left,
            "message": f"需要人工补充：{'、'.join(missing)}。" if missing else "",
            "task": None,
        }

    # ACTION_ESCALATE：唯一会改动数据库的分支
    updated = sqlite_db.update_risk_level(
        int(task_id),
        ESCALATE_TARGET_RISK,
        operator=operator,
        remark=remark,
        db_path=db_path,
    )
    return {
        "action": ACTION_ESCALATE,
        "applied": True,
        "task_id": task_id,
        "level": level,
        "days_left": days_left,
        "message": f"风险等级已提升为 {ESCALATE_TARGET_RISK}。",
        "task": updated,
    }
