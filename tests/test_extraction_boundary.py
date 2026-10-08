"""P0 修复专项测试：新事项边界判定（P0-1）与状态识别（P0-2）。

修复背景
--------
`agent/task_agent.normalize_tasks()` 此前把 status 无条件写死为 pending，
Prompt 也要求「固定返回 pending」。二者叠加造成一个严重缺陷：

    一份「工作进展 / 完成情况 / 阶段性汇报」材料中的 30 条已完成工作，
    全部被强制变成 30 条「待办」任务。

即：**不是模型凭空编造了任务，而是代码把「已完成」改写成了「待办」**，
凭空制造出材料中并不存在的执行压力。P0-1（不要生成虚假任务）与
P0-2（正确识别状态）因此是同一根因的两面。

本文件的测试全部注入假模型，不调用真实 API。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import task_agent
from config import settings
from llm import prompts


# ============================================================
# 假模型
# ============================================================


class FakeLLM:
    """记录调用次数并回放预置结果的假模型。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        self.calls += 1
        return self.payload


def _extract(payload: Any, text: str = "自检样例文本") -> list[dict[str, Any]]:
    return task_agent.extract_tasks(text, source_type="document", llm_fn=FakeLLM(payload))


# ============================================================
# 一、P0-1：材料无新执行事项时必须返回空列表
# ============================================================


def test_progress_report_with_only_completed_work_yields_nothing() -> None:
    """P0-1 核心场景：纯完成情况汇报不得生成任何任务。"""
    assert _extract([]) == []


def test_empty_array_payload_never_fabricates_tasks() -> None:
    """模型返回 [] 时结果必须是空列表，禁止「至少产出一条」的兜底逻辑。"""
    assert _extract([]) == []


def test_null_payload_is_rejected_as_protocol_violation() -> None:
    """模型返回 null 属于协议违规，必须报错，而不是当成「无任务」。

    「没有新事项」的正确表达是**空数组 []**；返回 null 说明模型没按格式输出，
    此时静默当成空列表会掩盖故障，因此沿用既有的类型校验直接报错。
    """
    with pytest.raises(ValueError):
        _extract(None)


def test_items_without_title_are_dropped() -> None:
    """没有标题的条目不构成可执行事项，应被丢弃。"""
    assert _extract([{"title": None, "status": "completed"}, {"title": "   "}]) == []


def test_cancelled_items_are_dropped() -> None:
    """P0-2：材料明确「已取消 / 已作废」的事项没有后续执行动作，不入任务库。"""
    tasks = _extract(
        [
            {"title": "校园文化节筹备", "status": "cancelled"},
            {"title": "教材征订", "status": "已取消"},
            {"title": "消防演练", "status": "已作废"},
        ]
    )
    assert tasks == []


def test_cancelled_does_not_affect_other_items() -> None:
    """已取消项被丢弃不应影响同一材料中的其他有效事项。"""
    tasks = _extract(
        [
            {"title": "已取消的旧任务", "status": "cancelled"},
            {"title": "需要继续推进的新任务", "status": "pending"},
        ]
    )
    assert [t["title"] for t in tasks] == ["需要继续推进的新任务"]


# ============================================================
# 二、P0-2：状态按材料明确表达识别
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("completed", "completed"),
        ("已完成", "completed"),
        ("已结束", "completed"),
        ("processing", "processing"),
        ("进行中", "processing"),
        ("正在推进", "processing"),
        ("pending", "pending"),
        ("待办", "pending"),
    ],
)
def test_status_recognized_from_chinese_and_english(raw: str, expected: str) -> None:
    """P0-2：状态中英文写法都能正确归一化。"""
    assert _extract([{"title": "某事项", "status": raw}])[0]["status"] == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "未知", "说不清", "PENDING"])
def test_unknown_status_falls_back_to_pending(raw: str) -> None:
    """P0-2.5：材料没写状态（或写法无法识别）时落回待办，不猜成已完成/进行中。"""
    assert _extract([{"title": "某事项", "status": raw}])[0]["status"] == "pending"


def test_status_is_never_inferred_from_other_fields() -> None:
    """P0-2.6：不允许由标题、部门、优先级反推状态。"""
    tasks = _extract(
        [
            {
                "title": "完成实验室安全台账整理",
                "department": "实验中心",
                "owner": "陈老师",
                "deadline": "2026-09-30",
                "priority": "high",
                "status": None,
            }
        ]
    )
    assert tasks[0]["status"] == "pending"


def test_completed_item_is_not_turned_into_pending() -> None:
    """P0-2.3 / P0-1.6：台账中明确标注已完成的事项保持 completed。

    这是 P0-1 的直接回归防线：此前它会被强制改成 pending，
    于是「已完成的历史工作」变成了「凭空新增的待办任务」。
    """
    tasks = _extract(
        [
            {"title": "上季度消防演练", "status": "completed"},
            {"title": "本季度台账复核", "status": "pending"},
        ]
    )
    by_title = {t["title"]: t for t in tasks}
    assert by_title["上季度消防演练"]["status"] == "completed"
    assert by_title["本季度台账复核"]["status"] == "pending"


def test_mixed_material_keeps_all_explicit_statuses() -> None:
    """混合材料：已完成 / 进行中 / 待办 三类状态并存时各自如实保留。"""
    tasks = _extract(
        [
            {"title": "A 已完成事项", "status": "completed"},
            {"title": "B 进行中事项", "status": "processing"},
            {"title": "C 新待办事项", "status": "pending"},
        ]
    )
    assert [t["status"] for t in tasks] == ["completed", "processing", "pending"]


def test_status_normalizer_is_pure_mapping() -> None:
    """状态归一化只做写法映射，非法值返回 None 交由调用方落回默认。"""
    assert task_agent._normalize_status("已完成") == "completed"
    assert task_agent._normalize_status("胡说八道") is None
    assert task_agent._normalize_status(None) is None


def test_status_dropped_marker_is_cancelled() -> None:
    """被丢弃的状态标记就是 cancelled（项目未提供该任务状态取值）。"""
    assert task_agent.STATUS_DROPPED == "cancelled"
    assert task_agent.STATUS_DROPPED not in settings.TASK_STATUSES


def test_emitted_statuses_are_all_valid_enum_values() -> None:
    """输出的状态必须都是数据库允许的合法取值。"""
    for raw in ("completed", "processing", "pending", "已完成", "进行中", "未知", None):
        for task in _extract([{"title": "某事项", "status": raw}]):
            assert task["status"] in settings.TASK_STATUSES


# ============================================================
# 三、Prompt 约束：防止模型层面再次退化
# ============================================================


def test_prompt_no_longer_forces_pending() -> None:
    """Prompt 不得再要求「状态固定返回 pending」。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    assert "固定返回" not in system
    assert '固定返回 "pending"' not in system


def test_prompt_defines_status_field_rules() -> None:
    """Prompt 必须明确状态取值与判定依据。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    for token in ("completed", "processing", "cancelled", "pending"):
        assert token in system
    assert "推测状态" in system


def test_prompt_lists_forbidden_completed_tense() -> None:
    """Prompt 必须列出禁止转换为新任务的完成态表达。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    for phrase in (
        "已完成",
        "已开展",
        "已落实",
        "工作成效",
        "工作进展",
        "已经进行了",
        "已取得",
        "已结束",
    ):
        assert phrase in system, f"Prompt 缺少禁止词：{phrase}"


def test_prompt_requires_semantic_not_keyword_judgment() -> None:
    """P0-1.7：必须结合上下文语义判断，而不是机械匹配关键词。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    assert "语义" in system
    assert "机械" in system


def test_prompt_requires_empty_array_when_no_new_items() -> None:
    """Prompt 必须要求「无新事项时返回空数组」且禁止硬凑。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    assert "空数组" in system
    assert "硬凑" in system or "凭空" in system


def test_prompt_covers_progress_report_material_type() -> None:
    """Prompt 必须覆盖「工作进展 / 完成情况 / 阶段性汇报」这一材料类型。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    for phrase in ("工作进展", "完成情况", "阶段性汇报"):
        assert phrase in system, f"Prompt 未覆盖材料类型：{phrase}"


def test_prompt_covers_ledger_material_type() -> None:
    """Prompt 必须覆盖「任务台账 / 计划清单」这一材料类型。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    for phrase in ("台账", "清单"):
        assert phrase in system


def test_prompt_states_prefer_fewer_over_fabricated() -> None:
    """Prompt 必须写明「宁可少输出，也绝不凭空生成」。"""
    system = prompts.TASK_EXTRACTION_SYSTEM
    assert "宁可少输出" in system
