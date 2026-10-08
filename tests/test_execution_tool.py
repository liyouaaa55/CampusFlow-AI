"""tools/execution_tool.py 单元测试（v2.0 P0-3）。

测试策略：
1. **以纯函数为主**——select_action / build_remark / build_params / normalize_plan
   不接触数据库，全部直接断言输出；
2. validate_plan 覆盖七层校验的每一层，包含黑名单优先、参数形状、
   前置条件、禁止字段、重复处置、人工确认；
3. execute 只验证其**能力边界**（改什么、不改什么），不重复验证内部实现；
4. 全部时间相关断言都注入固定的基准日 BASE_DATE，不依赖系统日期。

所有任务数据均为演示数据（Synthetic Data），每个测试使用 tmp_path 下的
独立数据库，不触碰真实的 data/campusflow.db。
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from database import models, sqlite_db
from tools import execution_tool as et

# ============================================================
# 固定基准数据（不依赖系统日期）
# ============================================================

# 基准日：所有 level 判定都基于这一天
BASE_DATE = date(2026, 10, 5)

# 各种提醒等级对应的截止日（相对 BASE_DATE）
DEADLINE_OVERDUE = "2026-10-03"   # days_left = -2 -> overdue
DEADLINE_URGENT = "2026-10-06"    # days_left = +1 -> urgent
DEADLINE_DUE_SOON = "2026-10-07"  # days_left = +2 -> due_soon
DEADLINE_NORMAL = "2026-10-20"    # days_left = +15 -> normal
DEADLINE_FUTURE = "2099-12-31"    # 远期，用于不注入 today 的稳定性场景


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """创建并初始化一个临时数据库，返回其路径。"""
    db_path = tmp_path / "execution_test.db"
    sqlite_db.init_db(db_path)
    return db_path


def make_task(**overrides: Any) -> dict[str, Any]:
    """构造一条标准任务（字段与 sqlite_db.get_task 返回结构一致）。"""
    task: dict[str, Any] = {
        "id": 1,
        "title": "宿舍消防隐患排查",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": DEADLINE_NORMAL,
        "priority": "high",
        "status": "pending",
        "risk_level": "green",
        "need_confirm": False,
        "description": "建立整改台账并完成隐患销号",
        "created_at": "2026-10-01 09:00:00",
        "updated_at": "2026-10-01 09:00:00",
    }
    task.update(overrides)
    return task


def make_log(action: str, days_ago: int = 1) -> dict[str, Any]:
    """构造一条 task_logs 记录（created_at 相对 BASE_DATE 回溯 days_ago 天）。"""
    day = (BASE_DATE - timedelta(days=days_ago)).isoformat()
    return {
        "id": 1,
        "task_id": 1,
        "action": action,
        "operator": "张老师",
        "remark": "",
        "created_at": f"{day} 09:00:00",
    }


def make_plan(
    action: Any,
    params: dict[str, Any] | None = None,
    need_confirm: Any = True,
) -> dict[str, Any]:
    """构造一份最小处置计划。"""
    return {
        "action": action,
        "params": {} if params is None else params,
        "need_confirm": need_confirm,
    }


# ============================================================
# 一、动作常量契约
# ============================================================


def test_allowed_actions_exactly_three() -> None:
    """v2.0 第一阶段只允许三个受控动作。"""
    assert et.ALLOWED_ACTIONS == ("DRAFT_NOTIFY", "REQUEST_INFO", "ESCALATE")
    assert et.ACTION_DRAFT_NOTIFY == "DRAFT_NOTIFY"
    assert et.ACTION_REQUEST_INFO == "REQUEST_INFO"
    assert et.ACTION_ESCALATE == "ESCALATE"


def test_blacklist_covers_all_forbidden_capabilities() -> None:
    """黑名单必须覆盖：标记完成 / 改负责人 / 改截止 / 改标题 / 改状态 / 外部发送。"""
    assert {
        "MARK_COMPLETED",
        "MODIFY_OWNER",
        "MODIFY_DEADLINE",
        "MODIFY_TITLE",
        "MODIFY_STATUS",
        "SEND_EXTERNAL",
    } <= set(et.BLACKLISTED_ACTIONS)


def test_blacklist_disjoint_from_allowed() -> None:
    """黑名单与允许集合不得有交集。"""
    assert set(et.ALLOWED_ACTIONS).isdisjoint(et.BLACKLISTED_ACTIONS)


def test_forbidden_param_fields_cover_protected_columns() -> None:
    """params 禁止字段必须覆盖全部受保护的任务列。"""
    assert set(et.FORBIDDEN_PARAM_FIELDS) == {
        "owner",
        "department",
        "deadline",
        "title",
        "status",
    }


def test_action_log_event_mapping_uses_models_constants() -> None:
    """业务动作与日志事件的映射必须对齐 models 中已定义的契约。"""
    assert et.ACTION_LOG_EVENT == {
        "DRAFT_NOTIFY": models.ACTION_DRAFT,
        "REQUEST_INFO": models.ACTION_REQUEST,
        "ESCALATE": models.ACTION_ESCALATE,
    }


def test_dedup_and_non_dedup_events_partition_event_actions() -> None:
    """去重事件与非去重事件合起来恰好覆盖 models.EVENT_ACTIONS。"""
    assert et.DEDUP_EVENTS == {"draft", "request", "escalate"}
    assert et.NON_DEDUP_EVENTS == {"analyze", "confirm", "reject"}
    assert et.DEDUP_EVENTS | et.NON_DEDUP_EVENTS == set(models.EVENT_ACTIONS)
    assert et.DEDUP_EVENTS & et.NON_DEDUP_EVENTS == set()


# ============================================================
# 二、select_action 决策规则（用例 1~8）
# ============================================================


def test_select_action_completed_returns_none() -> None:
    """用例 1：任务已完成 -> 无需处置。"""
    task = make_task(status="completed", deadline=DEADLINE_OVERDUE)
    assert et.select_action("overdue", task) is None


def test_select_action_missing_owner_returns_request_info() -> None:
    """用例 2：负责人缺失 -> REQUEST_INFO。"""
    task = make_task(owner=None, deadline=DEADLINE_OVERDUE)
    assert et.select_action("overdue", task) == et.ACTION_REQUEST_INFO


def test_select_action_missing_deadline_returns_request_info() -> None:
    """用例 3：截止时间缺失 -> REQUEST_INFO。"""
    task = make_task(deadline=None)
    assert et.select_action("urgent", task) == et.ACTION_REQUEST_INFO


def test_select_action_blank_owner_treated_as_missing() -> None:
    """空字符串与纯空白同样按缺失处理（与项目既有口径一致）。"""
    assert et.select_action("urgent", make_task(owner="   ")) == et.ACTION_REQUEST_INFO
    assert et.select_action("urgent", make_task(deadline="")) == et.ACTION_REQUEST_INFO


def test_select_action_overdue_not_red_returns_escalate() -> None:
    """用例 4：已逾期且尚未提级 -> ESCALATE。"""
    task = make_task(deadline=DEADLINE_OVERDUE, risk_level="green")
    assert et.select_action("overdue", task) == et.ACTION_ESCALATE


def test_select_action_overdue_red_downgrades_to_draft_notify() -> None:
    """用例 5：已逾期但风险等级已是 red -> 降级为 DRAFT_NOTIFY，仍产出可用文案。"""
    task = make_task(deadline=DEADLINE_OVERDUE, risk_level="red")
    assert et.select_action("overdue", task) == et.ACTION_DRAFT_NOTIFY


def test_select_action_due_soon_returns_draft_notify() -> None:
    """用例 6：临期 -> DRAFT_NOTIFY。"""
    task = make_task(deadline=DEADLINE_DUE_SOON)
    assert et.select_action("due_soon", task) == et.ACTION_DRAFT_NOTIFY


def test_select_action_urgent_returns_draft_notify() -> None:
    """用例 7：紧急 -> DRAFT_NOTIFY。"""
    task = make_task(deadline=DEADLINE_URGENT)
    assert et.select_action("urgent", task) == et.ACTION_DRAFT_NOTIFY


def test_select_action_normal_returns_none() -> None:
    """用例 8：正常 -> 无需处置。"""
    assert et.select_action("normal", make_task()) is None


def test_select_action_unknown_or_empty_level_returns_none() -> None:
    """未知 / 空等级不得凭空产生动作。"""
    assert et.select_action(None, make_task()) is None
    assert et.select_action("", make_task()) is None
    assert et.select_action("不存在的等级", make_task()) is None


def test_select_action_is_pure_and_repeatable() -> None:
    """纯函数：输入相同则输出相同，且不改动传入的任务字典。"""
    task = make_task(deadline=DEADLINE_OVERDUE)
    snapshot = dict(task)
    results = {et.select_action("overdue", task) for _ in range(5)}
    assert results == {et.ACTION_ESCALATE}
    assert task == snapshot


def test_select_action_rejects_non_mapping_task() -> None:
    """任务类型错误属于调用方契约违规，应显式抛异常。"""
    with pytest.raises(TypeError):
        et.select_action("overdue", ["not", "a", "mapping"])  # type: ignore[arg-type]


# ============================================================
# 三、resolve_level（复用 ReminderAgent，不另建规则）
# ============================================================


@pytest.mark.parametrize(
    ("deadline", "expected_level"),
    [
        (DEADLINE_OVERDUE, "overdue"),
        (DEADLINE_URGENT, "urgent"),
        (DEADLINE_DUE_SOON, "due_soon"),
        (DEADLINE_NORMAL, "normal"),
    ],
)
def test_resolve_level_matches_reminder_agent(deadline: str, expected_level: str) -> None:
    """等级判定必须与 ReminderAgent 完全一致。"""
    level, _days = et.resolve_level(make_task(deadline=deadline), BASE_DATE)
    assert level == expected_level


def test_resolve_level_missing_deadline_returns_empty() -> None:
    """缺少截止时间时无法判定等级，返回空等级与 None 天数。"""
    assert et.resolve_level(make_task(deadline=None), BASE_DATE) == ("", None)


def test_resolve_level_defaults_to_today() -> None:
    """不注入 today 时使用系统日期；远期截止日因此稳定落在 normal。"""
    level, days = et.resolve_level(make_task(deadline=DEADLINE_FUTURE))
    assert level == "normal"
    assert days is not None and days > 0


# ============================================================
# 四、build_params / build_remark（用例 26~29）
# ============================================================


def test_build_params_shapes() -> None:
    """只有 ESCALATE 携带参数，且严格为 risk_level=red。"""
    assert et.build_params(et.ACTION_DRAFT_NOTIFY) == {}
    assert et.build_params(et.ACTION_REQUEST_INFO) == {}
    assert et.build_params(et.ACTION_ESCALATE) == {"risk_level": "red"}
    assert et.build_params(None) == {}


def test_build_remark_within_max_length() -> None:
    """用例 26：remark 长度不超过 180。"""
    remark = et.build_remark(
        et.ACTION_ESCALATE, "overdue", -2, "任务已逾期 2 天仍未推进" * 40
    )
    assert len(remark) <= et.REMARK_MAX_LENGTH
    assert et.REMARK_MAX_LENGTH == 180


def test_build_remark_contains_action_and_level() -> None:
    """用例 27 / 28：remark 必须包含动作与等级，便于审计检索。"""
    remark = et.build_remark(et.ACTION_ESCALATE, "overdue", -2, "已逾期未推进")
    assert et.ACTION_ESCALATE in remark
    assert "overdue" in remark
    assert "days=-2" in remark


@pytest.mark.parametrize(
    ("action", "level", "days", "situation"),
    [
        (None, None, None, ""),
        (et.ACTION_DRAFT_NOTIFY, None, None, None),
        ("", "", None, "   "),
        (et.ACTION_REQUEST_INFO, "urgent", 1, "正常文本"),
    ],
)
def test_build_remark_handles_none_safely(
    action: Any, level: Any, days: Any, situation: Any
) -> None:
    """用例 29：None / 空值不得让 remark 构造函数崩溃。"""
    remark = et.build_remark(action, level, days, situation)
    assert isinstance(remark, str)
    assert remark
    assert len(remark) <= et.REMARK_MAX_LENGTH


def test_build_remark_uses_na_for_missing_days() -> None:
    """剩余天数缺失时使用 NA 占位，避免出现 None 字面量。"""
    assert "days=NA" in et.build_remark(et.ACTION_DRAFT_NOTIFY, "urgent", None, "")


def test_build_remark_non_positive_max_length() -> None:
    """max_length<=0 时返回空串而不是崩溃。"""
    assert et.build_remark(et.ACTION_ESCALATE, "overdue", -1, "x", max_length=0) == ""


# ============================================================
# 五、validate_plan —— L2 黑名单 / L1 枚举（用例 17~20）
# ============================================================


@pytest.mark.parametrize(
    "action",
    [
        et.BLACKLIST_MARK_COMPLETED,
        et.BLACKLIST_MODIFY_OWNER,
        et.BLACKLIST_MODIFY_DEADLINE,
        et.BLACKLIST_MODIFY_TITLE,
        et.BLACKLIST_MODIFY_STATUS,
        et.BLACKLIST_SEND_EXTERNAL,
    ],
)
def test_validate_plan_rejects_blacklisted_actions(action: str) -> None:
    """用例 17~20（并覆盖 MODIFY_TITLE / MODIFY_STATUS）：黑名单动作一律拒绝。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_OVERDUE, risk_level="green"),
        make_plan(action),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "黑名单" in result.reason


def test_blacklist_check_precedes_enum_check() -> None:
    """黑名单优先于允许集合：即便动作被误加进 ALLOWED_ACTIONS 也会被拒。"""
    assert et.ACTION_ESCALATE in et.ALLOWED_ACTIONS
    # 直接把一个黑名单值塞进允许集合，模拟配置被污染的情形
    original = et.ALLOWED_ACTIONS
    try:
        et.ALLOWED_ACTIONS = original + (et.BLACKLIST_MARK_COMPLETED,)
        result = et.validate_plan(
            make_task(deadline=DEADLINE_OVERDUE),
            make_plan(et.BLACKLIST_MARK_COMPLETED, {"status": "completed"}),
            [],
            today=BASE_DATE,
        )
        assert result.ok is False
        assert "黑名单" in result.reason
    finally:
        et.ALLOWED_ACTIONS = original


@pytest.mark.parametrize("action", ["MARK_DONE", "delete_task", "", None, "draft"])
def test_validate_plan_rejects_unknown_actions(action: Any) -> None:
    """非允许集合内的动作一律拒绝，并给出允许值提示。"""
    result = et.validate_plan(make_task(), make_plan(action), [], today=BASE_DATE)
    assert result.ok is False
    assert "允许集合" in result.reason


# ============================================================
# 六、validate_plan —— L3 参数（用例 9~11）
# ============================================================


def test_validate_plan_rejects_draft_notify_with_params() -> None:
    """用例 9：DRAFT_NOTIFY 不接受任何参数。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        make_plan(et.ACTION_DRAFT_NOTIFY, {"channel": "sms"}),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "不接受任何参数" in result.reason


def test_validate_plan_rejects_request_info_with_params() -> None:
    """用例 10：REQUEST_INFO 不接受任何参数。"""
    result = et.validate_plan(
        make_task(owner=None),
        make_plan(et.ACTION_REQUEST_INFO, {"owner": "王老师"}),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "不接受任何参数" in result.reason


@pytest.mark.parametrize("level", ["yellow", "green", "urgent", "overdue", "RED", ""])
def test_validate_plan_rejects_escalate_with_wrong_risk(level: str) -> None:
    """用例 11：ESCALATE 的 risk_level 只能是 red。

    特别覆盖 urgent / overdue：它们是基于 deadline 的时间层级，不是风险等级。
    """
    result = et.validate_plan(
        make_task(deadline=DEADLINE_OVERDUE, risk_level="green"),
        make_plan(et.ACTION_ESCALATE, {"risk_level": level}),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "risk_level" in result.reason


def test_validate_plan_rejects_escalate_with_extra_params() -> None:
    """ESCALATE 的 params 必须严格等于 {risk_level: red}，多一个键都不行。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_OVERDUE, risk_level="green"),
        make_plan(et.ACTION_ESCALATE, {"risk_level": "red", "status": "urgent"}),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False


def test_validate_plan_rejects_non_mapping_params() -> None:
    """params 类型错误属于结构问题，直接拒绝。"""
    plan = {"action": et.ACTION_DRAFT_NOTIFY, "params": ["red"], "need_confirm": True}
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON), plan, [], today=BASE_DATE
    )
    assert result.ok is False
    assert "params" in result.reason


def test_validate_plan_treats_missing_params_as_empty() -> None:
    """未提供 params 视为空字典，便于最小计划直接校验。"""
    plan = {"action": et.ACTION_DRAFT_NOTIFY, "need_confirm": True}
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON), plan, [], today=BASE_DATE
    )
    assert result.ok is True


# ============================================================
# 七、validate_plan —— L4 前置条件（用例 12~13）
# ============================================================


def test_validate_plan_rejects_escalate_when_already_red() -> None:
    """用例 12：风险等级已是 red 时不得重复提级。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_OVERDUE, risk_level="red"),
        make_plan(et.ACTION_ESCALATE, {"risk_level": "red"}),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "无需重复提级" in result.reason


@pytest.mark.parametrize("deadline", [DEADLINE_DUE_SOON, DEADLINE_URGENT, DEADLINE_NORMAL])
def test_validate_plan_rejects_escalate_when_not_overdue(deadline: str) -> None:
    """用例 13 之一：未逾期时不允许提级。"""
    result = et.validate_plan(
        make_task(deadline=deadline, risk_level="green"),
        make_plan(et.ACTION_ESCALATE, {"risk_level": "red"}),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "只有已逾期" in result.reason


def test_validate_plan_rejects_completed_task_for_every_action() -> None:
    """用例 13 之二：已完成任务对任何动作都不可处置。"""
    for action, params in (
        (et.ACTION_DRAFT_NOTIFY, {}),
        (et.ACTION_REQUEST_INFO, {}),
        (et.ACTION_ESCALATE, {"risk_level": "red"}),
    ):
        result = et.validate_plan(
            make_task(status="completed", owner=None, deadline=DEADLINE_OVERDUE),
            make_plan(action, params),
            [],
            today=BASE_DATE,
        )
        assert result.ok is False, f"{action} 不应通过"
        assert "已完成" in result.reason


def test_validate_plan_rejects_draft_notify_when_normal() -> None:
    """正常任务不需要督办提醒。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_NORMAL),
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [],
        today=BASE_DATE,
    )
    assert result.ok is False


def test_validate_plan_rejects_request_info_when_fields_complete() -> None:
    """负责人与截止时间齐备时，无需请求补充信息。"""
    result = et.validate_plan(
        make_task(), make_plan(et.ACTION_REQUEST_INFO), [], today=BASE_DATE
    )
    assert result.ok is False
    assert "无需请求补充信息" in result.reason


def test_validate_plan_ignores_level_supplied_by_plan() -> None:
    """plan 里的 level 一律不可信，必须由任务与基准日重新计算。

    伪造 level="overdue" 试图让未逾期任务通过提级校验，必须失败。
    """
    plan = make_plan(et.ACTION_ESCALATE, {"risk_level": "red"})
    plan["level"] = "overdue"
    result = et.validate_plan(
        make_task(deadline=DEADLINE_NORMAL, risk_level="green"),
        plan,
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "只有已逾期" in result.reason


# ============================================================
# 八、validate_plan —— L5 禁止字段（用例 15~16）
# ============================================================


@pytest.mark.parametrize("field", ["owner", "deadline"])
def test_validate_plan_rejects_protected_fields_in_params(field: str) -> None:
    """用例 15 / 16：params 出现受保护字段一律拒绝。

    注意：实际拦截发生在 L3（params 形状必须精确匹配），L5 是纵深防御。
    两层任一命中都满足「绝不允许修改这些字段」的要求。
    """
    for action, base_params in (
        (et.ACTION_DRAFT_NOTIFY, {}),
        (et.ACTION_REQUEST_INFO, {}),
        (et.ACTION_ESCALATE, {"risk_level": "red"}),
    ):
        params = dict(base_params)
        params[field] = "2026-12-31"
        result = et.validate_plan(
            make_task(deadline=DEADLINE_OVERDUE, risk_level="green"),
            make_plan(action, params),
            [],
            today=BASE_DATE,
        )
        assert result.ok is False, f"{action} 携带 {field} 时不应通过"


def test_forbidden_fields_are_declared_even_if_params_shape_relaxes() -> None:
    """纵深防御自证：受保护字段清单完整且不含允许字段。"""
    assert set(et.FORBIDDEN_PARAM_FIELDS).isdisjoint({"risk_level"})


# ============================================================
# 九、validate_plan —— L6 重复处置（用例 21~25）
# ============================================================


@pytest.mark.parametrize(
    ("action", "event"),
    [
        (et.ACTION_DRAFT_NOTIFY, models.ACTION_DRAFT),
        (et.ACTION_REQUEST_INFO, models.ACTION_REQUEST),
        (et.ACTION_ESCALATE, models.ACTION_ESCALATE),
    ],
)
def test_validate_plan_rejects_recent_same_action(action: str, event: str) -> None:
    """用例 21~23：最近窗口内已有同动作事件 -> 拒绝重复处置。"""
    task = make_task(owner=None, deadline=DEADLINE_OVERDUE, risk_level="green")
    params = {"risk_level": "red"} if action == et.ACTION_ESCALATE else {}
    result = et.validate_plan(
        task, make_plan(action, params), [make_log(event, days_ago=1)], today=BASE_DATE
    )
    assert result.ok is False
    assert "不重复处置" in result.reason


def test_validate_plan_allows_action_outside_dedup_window() -> None:
    """超出 ACTION_DEDUP_DAYS 窗口的同动作事件不再拦截。"""
    task = make_task(owner=None, deadline=DEADLINE_OVERDUE, risk_level="green")
    result = et.validate_plan(
        task,
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [make_log(models.ACTION_DRAFT, days_ago=et.ACTION_DEDUP_DAYS + 1)],
        today=BASE_DATE,
    )
    assert result.ok is True


def test_dedup_window_boundary_is_inclusive() -> None:
    """恰好第 N 天的事件仍算「最近」，应被拦截。"""
    task = make_task(deadline=DEADLINE_DUE_SOON)
    result = et.validate_plan(
        task,
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [make_log(models.ACTION_DRAFT, days_ago=et.ACTION_DEDUP_DAYS)],
        today=BASE_DATE,
    )
    assert result.ok is False


def test_validate_plan_analyze_log_does_not_block() -> None:
    """用例 24：只有 analyze 日志时不构成重复处置依据，允许再次分析。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [make_log(models.ACTION_ANALYZE, days_ago=0)],
        today=BASE_DATE,
    )
    assert result.ok is True


def test_validate_plan_analyze_then_reject_allows_new_attempt() -> None:
    """用例 25：analyze -> reject 之后必须允许重新提出方案。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [
            make_log(models.ACTION_ANALYZE, days_ago=1),
            make_log(models.ACTION_CONFIRM, days_ago=1),
            make_log(models.ACTION_REJECT, days_ago=1),
        ],
        today=BASE_DATE,
    )
    assert result.ok is True


def test_validate_plan_confirm_alone_does_not_block() -> None:
    """confirm 只表示人工确认过，不构成重复动作依据。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [make_log(models.ACTION_CONFIRM, days_ago=0)],
        today=BASE_DATE,
    )
    assert result.ok is True


def test_validate_plan_ignores_unparsable_log_timestamp() -> None:
    """时间戳无法解析时保守跳过，不让脏数据把流程永久锁死。"""
    broken = make_log(models.ACTION_DRAFT)
    broken["created_at"] = "不是时间"
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [broken],
        today=BASE_DATE,
    )
    assert result.ok is True


def test_validate_plan_ignores_malformed_log_entries() -> None:
    """日志列表中混入非映射元素时不得崩溃。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        make_plan(et.ACTION_DRAFT_NOTIFY),
        [None, "脏数据", 42],  # type: ignore[list-item]
        today=BASE_DATE,
    )
    assert result.ok is True


# ============================================================
# 十、validate_plan —— L7 人工确认（用例 14）
# ============================================================


@pytest.mark.parametrize("need_confirm", [False, None, 0, 1, "true", "True"])
def test_validate_plan_requires_strict_true_for_confirm(need_confirm: Any) -> None:
    """用例 14：need_confirm 必须严格为 True。

    注意 1 与 "true" 也被拒绝——本项目要求严格布尔，避免 JSON 数字被当作同意。
    """
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        {"action": et.ACTION_DRAFT_NOTIFY, "params": {}, "need_confirm": need_confirm},
        [],
        today=BASE_DATE,
    )
    assert result.ok is False
    assert "人工确认" in result.reason


def test_validate_plan_missing_need_confirm_is_rejected() -> None:
    """未提供 need_confirm 视为未确认，拒绝执行。"""
    result = et.validate_plan(
        make_task(deadline=DEADLINE_DUE_SOON),
        {"action": et.ACTION_DRAFT_NOTIFY, "params": {}},
        [],
        today=BASE_DATE,
    )
    assert result.ok is False


# ============================================================
# 十一、validate_plan 通过路径
# ============================================================


@pytest.mark.parametrize(
    ("action", "task_overrides", "params"),
    [
        (et.ACTION_DRAFT_NOTIFY, {"deadline": DEADLINE_DUE_SOON}, {}),
        (et.ACTION_DRAFT_NOTIFY, {"deadline": DEADLINE_URGENT}, {}),
        (et.ACTION_DRAFT_NOTIFY, {"deadline": DEADLINE_OVERDUE, "risk_level": "red"}, {}),
        (et.ACTION_REQUEST_INFO, {"owner": None}, {}),
        (et.ACTION_REQUEST_INFO, {"deadline": None}, {}),
        (et.ACTION_ESCALATE, {"deadline": DEADLINE_OVERDUE, "risk_level": "green"}, {"risk_level": "red"}),
        (et.ACTION_ESCALATE, {"deadline": DEADLINE_OVERDUE, "risk_level": "yellow"}, {"risk_level": "red"}),
        (et.ACTION_ESCALATE, {"deadline": DEADLINE_OVERDUE, "risk_level": None}, {"risk_level": "red"}),
    ],
)
def test_validate_plan_accepts_legitimate_plans(
    action: str, task_overrides: dict[str, Any], params: dict[str, Any]
) -> None:
    """合法组合必须全部通过，否则 Validator 会把正常流程也拦死。"""
    result = et.validate_plan(
        make_task(**task_overrides), make_plan(action, params), [], today=BASE_DATE
    )
    assert result.ok is True, result.reason
    assert result.action == action


def test_validation_result_is_frozen() -> None:
    """ValidationResult 为不可变值对象，避免被调用方事后篡改。"""
    result = et.ValidationResult(ok=True, reason="x", action=None)
    with pytest.raises(Exception):
        result.ok = False  # type: ignore[misc]


def test_validate_plan_never_raises_for_business_rejections() -> None:
    """业务拒绝一律通过 ValidationResult 表达，不抛异常。"""
    for action in list(et.BLACKLISTED_ACTIONS) + ["UNKNOWN", None]:
        result = et.validate_plan(make_task(), make_plan(action), [], today=BASE_DATE)
        assert isinstance(result, et.ValidationResult)
        assert result.ok is False


# ============================================================
# 十二、normalize_plan：绝不相信外部 action / params
# ============================================================


def test_normalize_plan_uses_rule_decided_action() -> None:
    """动作来自 select_action()，而不是外部输入。"""
    plan = et.normalize_plan(
        make_task(deadline=DEADLINE_OVERDUE, risk_level="green"),
        "overdue",
        raw={"situation": "已逾期 2 天", "reasons": ["截止时间已过"]},
    )
    assert plan["action"] == et.ACTION_ESCALATE
    assert plan["params"] == {"risk_level": "red"}
    assert plan["need_confirm"] is True
    assert plan["situation"] == "已逾期 2 天"
    assert plan["reasons"] == ["截止时间已过"]


def test_normalize_plan_discards_llm_proposed_action_and_params() -> None:
    """关键安全约束：大模型返回的动作与参数必须被丢弃。

    这是「谁决定做什么」可追溯的核心保障：模型说 MARK_COMPLETED 也无效。
    """
    hostile_raw = {
        "action": et.BLACKLIST_MARK_COMPLETED,
        "params": {"status": "completed", "owner": "我", "deadline": "2099-01-01"},
        "situation": "看起来已经做完了",
        "reasons": ["模型判断已完成"],
    }
    plan = et.normalize_plan(
        make_task(deadline=DEADLINE_DUE_SOON), "due_soon", raw=hostile_raw
    )

    assert plan["action"] == et.ACTION_DRAFT_NOTIFY  # 规则判定，而非 MARK_COMPLETED
    assert plan["params"] == {}
    # 计划中不得残留任何受保护字段
    assert set(plan["params"]) & set(et.FORBIDDEN_PARAM_FIELDS) == set()
    # 但允许保留模型的解释性文本
    assert plan["situation"] == "看起来已经做完了"


def test_normalize_plan_never_targets_status_field() -> None:
    """无论输入如何，产出的 params 中都不含 status。"""
    for level in ("normal", "due_soon", "urgent", "overdue"):
        plan = et.normalize_plan(make_task(deadline=DEADLINE_OVERDUE), level)
        assert "status" not in plan["params"]


def test_normalize_plan_no_action_yields_empty_string() -> None:
    """无需处置时 action 为空字符串（而非 None），保证下游类型稳定。"""
    plan = et.normalize_plan(make_task(), "normal")
    assert plan["action"] == ""
    assert plan["params"] == {}
    assert plan["risk_note"] == et.RISK_NOTES[""]


def test_normalize_plan_requires_mapping_task() -> None:
    """任务类型错误属于契约违规，抛异常。"""
    with pytest.raises(TypeError):
        et.normalize_plan("not a mapping", "urgent")  # type: ignore[arg-type]


def test_normalize_plan_output_passes_validation() -> None:
    """normalize_plan 的产出必须能通过自己的 Validator（闭环自证）。"""
    for level, deadline, risk in (
        ("overdue", DEADLINE_OVERDUE, "green"),
        ("due_soon", DEADLINE_DUE_SOON, "green"),
        ("urgent", DEADLINE_URGENT, "green"),
    ):
        task = make_task(deadline=deadline, risk_level=risk)
        plan = et.normalize_plan(task, level)
        result = et.validate_plan(task, plan, [], today=BASE_DATE)
        assert result.ok is True, f"{level}: {result.reason}"


# ============================================================
# 十三、execute 能力边界（用例 30~35）
# ============================================================


def test_execute_draft_notify_produces_text_without_sending(db: Path) -> None:
    """用例 30：DRAFT_NOTIFY 只返回提醒文案，不发送、不落库。"""
    task = make_task(deadline=DEADLINE_DUE_SOON)
    result = et.execute(et.ACTION_DRAFT_NOTIFY, task=task, today=BASE_DATE, db_path=db)

    assert result["action"] == et.ACTION_DRAFT_NOTIFY
    assert result["applied"] is False
    assert result["task"] is None
    assert result["message"]
    assert result["level"] == "due_soon"
    # 未产生任何数据库写入
    assert sqlite_db.list_tasks(db_path=db) == []
    assert result["message"]


def test_execute_has_no_external_send_capability() -> None:
    """结构自证：本模块不具备任何外发能力（纵深防御）。

    仿照 tests/test_report_agent.py 中 inspect.getsource 的既有范式。
    """
    source = inspect.getsource(et)
    for forbidden in ("smtplib", "sendmail", "SMTP(", "urllib", "http.client", "requests"):
        assert forbidden not in source, f"execution_tool 不应出现 {forbidden}"


def test_execute_request_info_does_not_modify_owner(db: Path) -> None:
    """用例 31：REQUEST_INFO 绝不修改 owner。"""
    created = sqlite_db.create_task(
        {"title": "整理档案", "owner": None, "deadline": DEADLINE_DUE_SOON}, db_path=db
    )
    before = sqlite_db.get_task(created["id"], db_path=db)

    result = et.execute(
        et.ACTION_REQUEST_INFO, task=before, today=BASE_DATE, db_path=db
    )

    after = sqlite_db.get_task(created["id"], db_path=db)
    assert result["applied"] is False
    assert after["owner"] is None
    assert after == before
    assert "owner" in result["message"]


def test_execute_request_info_does_not_modify_deadline(db: Path) -> None:
    """用例 32：REQUEST_INFO 绝不修改 deadline。"""
    created = sqlite_db.create_task(
        {"title": "整理档案", "owner": "王老师", "deadline": None}, db_path=db
    )
    before = sqlite_db.get_task(created["id"], db_path=db)

    result = et.execute(
        et.ACTION_REQUEST_INFO, task=before, today=BASE_DATE, db_path=db
    )

    after = sqlite_db.get_task(created["id"], db_path=db)
    assert result["applied"] is False
    assert after["deadline"] is None
    assert after == before
    assert "deadline" in result["message"]


def test_execute_escalate_calls_update_risk_level(
    monkeypatch: pytest.MonkeyPatch, db: Path
) -> None:
    """用例 33：ESCALATE 必须经由 sqlite_db.update_risk_level 执行。"""
    calls: list[dict[str, Any]] = []
    original = sqlite_db.update_risk_level

    def spy(task_id: int, risk_level: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"task_id": task_id, "risk_level": risk_level, **kwargs})
        return original(task_id, risk_level, **kwargs)

    monkeypatch.setattr(sqlite_db, "update_risk_level", spy)

    created = sqlite_db.create_task(
        {"title": "实验室安全检查", "deadline": DEADLINE_OVERDUE, "risk_level": "green"},
        db_path=db,
    )
    task = sqlite_db.get_task(created["id"], db_path=db)

    result = et.execute(
        et.ACTION_ESCALATE,
        task=task,
        params={"risk_level": "red"},
        operator="张老师",
        remark="[ESCALATE] level=overdue days=-2 | 逾期未推进",
        today=BASE_DATE,
        db_path=db,
    )

    assert len(calls) == 1
    assert calls[0]["task_id"] == created["id"]
    assert calls[0]["risk_level"] == "red"
    assert calls[0]["operator"] == "张老师"
    assert result["applied"] is True
    assert result["task"]["risk_level"] == "red"


def test_execute_escalate_writes_escalate_log_and_keeps_status(db: Path) -> None:
    """ESCALATE 的真实落库效果：risk_level 变 red、日志留痕、status 不变。"""
    created = sqlite_db.create_task(
        {
            "title": "实验室安全检查",
            "status": "processing",
            "deadline": DEADLINE_OVERDUE,
            "risk_level": "green",
        },
        db_path=db,
    )
    task = sqlite_db.get_task(created["id"], db_path=db)

    et.execute(
        et.ACTION_ESCALATE,
        task=task,
        params={"risk_level": "red"},
        remark="逾期 2 天未推进",
        today=BASE_DATE,
        db_path=db,
    )

    after = sqlite_db.get_task(created["id"], db_path=db)
    assert after["risk_level"] == "red"
    assert after["status"] == "processing"  # 绝不改成 urgent / overdue
    actions = [log["action"] for log in sqlite_db.get_logs(created["id"], db_path=db)]
    assert actions == [models.ACTION_CREATE, models.ACTION_ESCALATE]


@pytest.mark.parametrize(
    "action", ["UNKNOWN_ACTION", "", None, "draft", "mark_completed"]
)
def test_execute_rejects_unknown_action(action: Any, db: Path) -> None:
    """用例 34：execute 不允许未知动作。"""
    with pytest.raises(ValueError):
        et.execute(action, task=make_task(), today=BASE_DATE, db_path=db)


@pytest.mark.parametrize("action", sorted(et.BLACKLISTED_ACTIONS))
def test_execute_refuses_blacklisted_action(action: str, db: Path) -> None:
    """用例 35：execute 不能执行黑名单动作（最后一道防线）。"""
    with pytest.raises(ValueError):
        et.execute(action, task=make_task(), today=BASE_DATE, db_path=db)


def test_execute_escalate_rejects_wrong_params(db: Path) -> None:
    """execute 层同样校验参数形状，不依赖调用方已先 validate。"""
    with pytest.raises(ValueError):
        et.execute(
            et.ACTION_ESCALATE,
            task=make_task(deadline=DEADLINE_OVERDUE),
            params={"risk_level": "urgent"},
            today=BASE_DATE,
            db_path=db,
        )


def test_execute_requires_mapping_task(db: Path) -> None:
    """任务类型错误属于契约违规。"""
    with pytest.raises(TypeError):
        et.execute(et.ACTION_DRAFT_NOTIFY, task="not a mapping", db_path=db)  # type: ignore[arg-type]


def test_execute_source_never_writes_protected_fields() -> None:
    """结构自证：execute 不含任何修改受保护字段的数据库调用。"""
    source = inspect.getsource(et.execute)
    for forbidden in ("update_owner", "update_deadline", "update_status", "update_title"):
        assert forbidden not in source
    # 唯一允许的写入口是 update_risk_level
    assert "update_risk_level" in source


def test_execute_does_not_write_draft_log(db: Path) -> None:
    """draft / request 日志由调用方的处置流程写入，execute 不越权代写。"""
    created = sqlite_db.create_task(
        {"title": "实验室安全检查", "deadline": DEADLINE_DUE_SOON}, db_path=db
    )
    task = sqlite_db.get_task(created["id"], db_path=db)

    et.execute(et.ACTION_DRAFT_NOTIFY, task=task, today=BASE_DATE, db_path=db)
    et.execute(et.ACTION_REQUEST_INFO, task=task, today=BASE_DATE, db_path=db)

    actions = [log["action"] for log in sqlite_db.get_logs(created["id"], db_path=db)]
    assert actions == [models.ACTION_CREATE]
