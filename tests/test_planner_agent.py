"""PlannerAgent 单元测试。

测试策略：PlannerAgent 不调用 LLM，全部逻辑为确定性规则，因此测试不依赖网络。
基准日期固定为 2026-09-23（TODAY），保证「临期 / 逾期」判定稳定可复现。
所有任务数据均为演示数据（Synthetic Data）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from agent import planner_agent
from config import settings
from tools import priority_tool

# 固定基准日期，避免因运行时间不同导致结果漂移
TODAY = date(2026, 9, 23)


def make_task(**overrides: object) -> dict:
    """构造一条标准的 TaskAgent 输出（可覆盖任意字段）。"""
    task = {
        "title": "举办机器人创新周",
        "department": "学工办",
        "owner": "张老师",
        "deadline": "2026-09-30",
        "priority": "medium",
        "status": "pending",
        "description": "制定活动方案",
    }
    task.update(overrides)
    return task


def plan_one(task: dict) -> dict:
    """对单条任务执行规划（统一使用基准日期）。"""
    return planner_agent.plan_task(task, today=TODAY)


# ============================================================
# 一、输出结构与基础行为
# ============================================================


def test_output_field_order_matches_spec() -> None:
    """输出保持原字段顺序，并在末尾追加 risk_level / need_confirm / suggestions。"""
    planned = plan_one(make_task())
    assert list(planned.keys()) == [
        "title",
        "department",
        "owner",
        "deadline",
        "priority",
        "status",
        "description",
        "risk_level",
        "need_confirm",
        "suggestions",
    ]
    # 与模块常量保持一致
    assert tuple(planned.keys()) == planner_agent.OUTPUT_FIELDS
    assert planner_agent.OUTPUT_FIELDS[:7] == planner_agent.TASK_FIELDS


def test_complete_task_is_green_without_suggestions() -> None:
    """信息齐备且无风险的任务：green、无需确认、无建议。"""
    planned = plan_one(make_task(description="完成材料归档"))
    assert planned["risk_level"] == planner_agent.RISK_GREEN
    assert planned["need_confirm"] is False
    assert planned["suggestions"] == []


def test_input_dict_is_not_mutated() -> None:
    """规划过程不得修改入参（避免污染 TaskAgent 的输出缓存）。"""
    task = make_task(owner=None, priority=None)
    snapshot = json.loads(json.dumps(task, ensure_ascii=False))
    plan_one(task)
    assert task == snapshot


def test_result_is_json_serializable() -> None:
    """输出必须可直接序列化为 JSON（前端与数据库都依赖该结构）。"""
    planned = plan_one(make_task(owner=None, priority=None))
    assert json.loads(json.dumps(planned, ensure_ascii=False))["risk_level"] in planner_agent.RISK_LEVELS


def test_status_defaults_to_pending() -> None:
    """状态缺失或非法时回落为 pending，合法状态原样保留。"""
    assert plan_one(make_task(status=None))["status"] == settings.DEFAULT_STATUS
    assert plan_one(make_task(status="莫名其妙的写法"))["status"] == settings.DEFAULT_STATUS
    assert plan_one(make_task(status="processing"))["status"] == "processing"


def test_missing_title_raises_value_error() -> None:
    """无标题的任务无法规划，直接报错而不是产出空任务。"""
    with pytest.raises(ValueError):
        plan_one(make_task(title="   "))


def test_non_dict_item_raises_type_error() -> None:
    """列表中出现非字典元素时报错。"""
    with pytest.raises(TypeError):
        planner_agent.plan([{"title": "整理材料"}, "不是字典"])  # type: ignore[list-item]


def test_plan_accepts_single_dict_and_none() -> None:
    """plan() 兼容单条字典与 None 输入。"""
    single = planner_agent.plan({"title": "整理档案材料", "description": None}, today=TODAY)
    assert len(single) == 1
    assert planner_agent.plan(None) == []


# ============================================================
# 二、risk_level 风险等级
# ============================================================


def test_risk_yellow_when_owner_missing() -> None:
    """负责人缺失 → yellow。"""
    planned = plan_one(make_task(owner=None))
    assert planned["risk_level"] == planner_agent.RISK_YELLOW


def test_risk_yellow_when_priority_missing() -> None:
    """优先级缺失 → yellow（并用规则推断出具体优先级）。"""
    planned = plan_one(make_task(priority=None))
    assert planned["risk_level"] == planner_agent.RISK_YELLOW
    assert planned["priority"] == "medium"


def test_risk_red_when_deadline_missing() -> None:
    """截止时间缺失 → red。"""
    planned = plan_one(make_task(deadline=None))
    assert planned["risk_level"] == planner_agent.RISK_RED
    assert planned["deadline"] is None


def test_risk_red_when_overdue() -> None:
    """截止时间已过期 → red。"""
    overdue_day = (TODAY - timedelta(days=1)).isoformat()
    planned = plan_one(make_task(deadline=overdue_day))
    assert planned["risk_level"] == planner_agent.RISK_RED
    assert planner_agent.MSG_DEADLINE_OVERDUE in planned["suggestions"]


def test_deadline_today_is_not_overdue() -> None:
    """截止时间为今天：不算逾期，信息齐备时仍为 green。"""
    planned = plan_one(make_task(deadline=TODAY.isoformat()))
    assert planned["risk_level"] == planner_agent.RISK_GREEN
    assert planner_agent.MSG_DEADLINE_OVERDUE not in planned["suggestions"]


def test_red_takes_precedence_over_yellow() -> None:
    """同时缺负责人与截止时间时，取更严重的 red。"""
    planned = plan_one(make_task(owner=None, deadline=None))
    assert planned["risk_level"] == planner_agent.RISK_RED


def test_overdue_alone_does_not_require_confirm() -> None:
    """仅逾期（负责人/截止/优先级都齐备）时：risk_level 为 red，但不要求人工确认。

    需求明确：need_confirm 只在「负责人缺失 / 截止时间缺失 / 优先级缺失」时为 True，
    逾期属于执行风险，通过 risk_level 与 suggestions 传达。
    """
    planned = plan_one(make_task(deadline=(TODAY - timedelta(days=2)).isoformat()))
    assert planned["risk_level"] == planner_agent.RISK_RED
    assert planned["need_confirm"] is False
    assert planner_agent.MSG_DEADLINE_OVERDUE in planned["suggestions"]


@pytest.mark.parametrize("raw_deadline", ["下周三", "尽快完成", "待定", "2026-13-45", ""])
def test_invalid_deadline_treated_as_missing(raw_deadline: str) -> None:
    """无法解析为具体日期的截止时间按缺失处理（red），不做日期推断。"""
    planned = plan_one(make_task(deadline=raw_deadline))
    assert planned["deadline"] is None
    assert planned["risk_level"] == planner_agent.RISK_RED


@pytest.mark.parametrize(
    ("owner", "deadline", "priority", "expected_risk", "expected_confirm"),
    [
        ("张老师", "2026-09-30", "high", planner_agent.RISK_GREEN, False),
        (None, "2026-09-30", "high", planner_agent.RISK_YELLOW, True),
        ("张老师", "2026-09-30", None, planner_agent.RISK_YELLOW, True),
        ("张老师", None, "high", planner_agent.RISK_RED, True),
        (None, None, None, planner_agent.RISK_RED, True),
        # 仅逾期（字段齐备）：风险为 red，但不属于「三项缺失」，因此无需人工确认
        ("张老师", "2026-09-01", "high", planner_agent.RISK_RED, False),
    ],
)
def test_risk_and_confirm_matrix(
    owner: str | None,
    deadline: str | None,
    priority: str | None,
    expected_risk: str,
    expected_confirm: bool,
) -> None:
    """风险等级与人工确认标记的组合矩阵。"""
    planned = plan_one(make_task(owner=owner, deadline=deadline, priority=priority))
    assert planned["risk_level"] == expected_risk
    assert planned["need_confirm"] is expected_confirm


# ============================================================
# 三、need_confirm 人工确认
# ============================================================


def test_need_confirm_true_when_owner_missing() -> None:
    """负责人缺失 → 需要人工确认。"""
    assert plan_one(make_task(owner=None))["need_confirm"] is True


def test_need_confirm_true_when_deadline_missing() -> None:
    """截止时间缺失 → 需要人工确认。"""
    assert plan_one(make_task(deadline=None))["need_confirm"] is True


def test_need_confirm_true_when_priority_missing() -> None:
    """优先级缺失 → 需要人工确认。"""
    assert plan_one(make_task(priority=None))["need_confirm"] is True


def test_need_confirm_false_when_complete() -> None:
    """三项齐备 → 无需人工确认。"""
    assert plan_one(make_task(description="完成材料归档"))["need_confirm"] is False


# ============================================================
# 四、优先级规则（读取 knowledge/rules/priority.json）
# ============================================================


def test_safety_task_priority_is_high() -> None:
    """安全类事项 → high。"""
    planned = plan_one(make_task(title="完成宿舍消防隐患专项排查", priority=None))
    assert planned["priority"] == "high"
    assert any("「消防」" in item for item in planned["suggestions"])


@pytest.mark.parametrize("title", ["开展实验室安全检查", "启动防汛应急预案", "校园安全隐患排查"])
def test_high_priority_keywords(title: str) -> None:
    """实验室 / 应急 / 安全 关键词 → high。"""
    planned = plan_one(make_task(title=title, description=None, priority=None))
    assert planned["priority"] == "high"


def test_activity_task_priority_is_medium() -> None:
    """活动类事项 → medium。"""
    planned = plan_one(make_task(title="举办机器人创新周", description="制定活动方案", priority=None))
    assert planned["priority"] == "medium"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("开展期初教学检查", "medium"),
        ("加强宣传阵地建设", "medium"),
        ("发布停水通知", "low"),
    ],
)
def test_medium_and_low_priority_keywords(title: str, expected: str) -> None:
    """教学 / 宣传 → medium，纯通知 → low。"""
    planned = plan_one(make_task(title=title, description=None, priority=None))
    assert planned["priority"] == expected


def test_highest_level_wins() -> None:
    """同时命中多级关键词时取最高级别（就高不就低）。"""
    planned = plan_one(make_task(title="实验室安全通知", description=None, priority=None))
    assert planned["priority"] == "high"


def test_keywords_matched_from_description() -> None:
    """描述中的关键词同样参与判定（标题 + 描述一起匹配）。"""
    planned = plan_one(make_task(title="举办创新周", description="开展消防安全演练", priority=None))
    assert planned["priority"] == "high"


def test_default_priority_when_no_keyword() -> None:
    """未命中任何关键词 → 使用配置中的默认优先级。"""
    planned = plan_one(make_task(title="整理归档材料", description=None, priority=None))
    assert planned["priority"] == settings.DEFAULT_PRIORITY


def test_existing_priority_is_not_overwritten() -> None:
    """任务自带合法优先级时沿用，不被关键词规则覆盖。"""
    planned = plan_one(make_task(title="完成消防隐患专项排查", priority="low"))
    assert planned["priority"] == "low"
    # 但高风险关键词仍会给出优先处理建议
    assert any("「消防」" in item for item in planned["suggestions"])


def test_invalid_priority_falls_back_to_rules() -> None:
    """非法优先级视为缺失，由关键词规则推断，并需要人工确认。"""
    planned = plan_one(make_task(title="完成消防隐患专项排查", description=None, priority="P0"))
    assert planned["priority"] == "high"
    assert planned["need_confirm"] is True


def test_priority_case_insensitive() -> None:
    """优先级大小写不敏感，合法值不会被判定为缺失。"""
    planned = plan_one(make_task(description="完成材料归档", priority="HIGH"))
    assert planned["priority"] == "high"
    assert planned["need_confirm"] is False


# ============================================================
# 五、规则文件读取（不写死在代码里）
# ============================================================


def test_rules_loaded_from_knowledge_file() -> None:
    """规则文件应包含三级优先级与规定关键词，注释字段被忽略。"""
    rules = priority_tool.load_rules()
    assert set(rules) == set(settings.PRIORITIES)
    for keyword in ("消防", "实验室", "安全", "应急"):
        assert keyword in rules["high"]
    for keyword in ("教学", "活动", "宣传"):
        assert keyword in rules["medium"]
    assert "通知" in rules["low"]
    assert "_comment" not in rules


def test_rules_path_lives_in_knowledge_rules() -> None:
    """规则文件必须位于 knowledge/rules 下，供业务人员直接维护。"""
    assert priority_tool.PRIORITY_RULES_PATH == settings.RULES_DIR / "priority.json"
    assert priority_tool.PRIORITY_RULES_PATH.exists()


def test_missing_rules_file_raises(tmp_path) -> None:
    """规则文件缺失时抛出明确异常，而不是静默使用默认值。"""
    with pytest.raises(priority_tool.RulesError):
        priority_tool.load_rules(tmp_path / "not_exists.json")


@pytest.mark.parametrize(
    "content",
    [
        "{不是合法 JSON",
        '{"urgent": ["安全"]}',          # 未知优先级
        '{"high": "安全"}',              # 关键词不是数组
        '["安全"]',                      # 顶层不是对象
    ],
)
def test_invalid_rules_file_raises(tmp_path, content: str) -> None:
    """规则文件格式非法时报错。"""
    rules_file = tmp_path / "priority.json"
    rules_file.write_text(content, encoding="utf-8")
    with pytest.raises(priority_tool.RulesError):
        priority_tool.load_rules(rules_file)


def test_rules_are_configurable_not_hardcoded() -> None:
    """注入自定义规则即可改变判定结果，证明关键词未写死在代码中。"""
    custom_rules = {"high": ["绿化"], "medium": [], "low": []}

    # 自定义规则命中 → high
    planned = planner_agent.plan_task(
        make_task(title="修剪绿化带", description=None, priority=None),
        rules=custom_rules,
        today=TODAY,
    )
    assert planned["priority"] == "high"

    # 自定义规则中不含「活动」，因此回落到默认优先级
    fallback = planner_agent.plan_task(
        make_task(title="举办机器人创新周", description="制定活动方案", priority=None),
        rules=custom_rules,
        today=TODAY,
    )
    assert fallback["priority"] == settings.DEFAULT_PRIORITY


def test_rules_cache_and_force_reload(tmp_path) -> None:
    """规则按路径缓存；force_reload 可强制重新读取。"""
    rules_file = tmp_path / "priority.json"
    rules_file.write_text('{"high": ["绿化"], "medium": [], "low": []}', encoding="utf-8")
    assert "绿化" in priority_tool.load_rules(rules_file)["high"]

    # 修改文件后，缓存仍然有效
    rules_file.write_text('{"high": ["消防"], "medium": [], "low": []}', encoding="utf-8")
    assert "绿化" in priority_tool.load_rules(rules_file)["high"]

    # 强制重载后读到新内容
    assert "消防" in priority_tool.load_rules(rules_file, force_reload=True)["high"]


# ============================================================
# 六、suggestions 建议生成
# ============================================================


def test_suggestion_for_missing_owner() -> None:
    """负责人缺失给出指定文案。"""
    planned = plan_one(make_task(owner=None, description="完成材料归档"))
    assert planned["suggestions"] == [planner_agent.MSG_OWNER_MISSING]


def test_suggestion_for_missing_deadline() -> None:
    """截止时间缺失给出指定文案（且不重复给出临期建议）。"""
    planned = plan_one(make_task(deadline=None, description="完成材料归档"))
    assert planner_agent.MSG_DEADLINE_MISSING in planned["suggestions"]
    assert planner_agent.MSG_DEADLINE_DUE_SOON not in planned["suggestions"]


def test_suggestion_for_due_soon_deadline() -> None:
    """剩余天数不超过临期阈值时给出临近提醒。"""
    soon = (TODAY + timedelta(days=settings.REMINDER_DUE_SOON_DAYS)).isoformat()
    planned = plan_one(make_task(deadline=soon, description="完成材料归档"))
    assert planner_agent.MSG_DEADLINE_DUE_SOON in planned["suggestions"]


def test_suggestion_mentions_inferred_priority() -> None:
    """优先级由规则推断时，建议中说明推断结果。"""
    planned = plan_one(make_task(priority=None, description="制定活动方案"))
    assert planner_agent.MSG_PRIORITY_MISSING.format(level="medium") in planned["suggestions"]


def test_suggestions_are_string_list() -> None:
    """suggestions 必须是字符串数组。"""
    planned = plan_one(make_task(owner=None, deadline=None, priority=None))
    assert isinstance(planned["suggestions"], list)
    assert planned["suggestions"]
    assert all(isinstance(item, str) for item in planned["suggestions"])


def test_spec_example_output() -> None:
    """复现需求文档中的示例：负责人与优先级缺失 → yellow + 需确认 + 推断为 medium。"""
    task = {
        "title": "举办机器人创新周",
        "department": "学工办",
        "owner": None,
        "deadline": "2026-09-30",
        "priority": None,
        "status": "pending",
        "description": "制定活动方案",
    }
    planned = plan_one(task)

    assert planned["title"] == "举办机器人创新周"
    assert planned["priority"] == "medium"
    assert planned["risk_level"] == planner_agent.RISK_YELLOW
    assert planned["need_confirm"] is True
    assert planned["suggestions"][0] == planner_agent.MSG_OWNER_MISSING
    # 除负责人外，优先级同样缺失，因此额外给出一条推断说明
    assert planner_agent.MSG_PRIORITY_MISSING.format(level="medium") in planned["suggestions"]


# ============================================================
# 七、批量规划
# ============================================================


def test_plan_multiple_tasks_preserves_order() -> None:
    """批量规划：逐条独立判定，顺序保持不变。"""
    tasks = [
        make_task(title="完成消防隐患专项排查", owner="王老师", priority="high"),
        make_task(title="举办机器人创新周", owner=None, priority=None),
        make_task(title="整理归档材料", deadline=None, description=None),
    ]
    planned = planner_agent.plan(tasks, today=TODAY)

    assert [item["title"] for item in planned] == [
        "完成消防隐患专项排查",
        "举办机器人创新周",
        "整理归档材料",
    ]
    assert [item["risk_level"] for item in planned] == [
        planner_agent.RISK_GREEN,
        planner_agent.RISK_YELLOW,
        planner_agent.RISK_RED,
    ]
    assert [item["need_confirm"] for item in planned] == [False, True, True]
