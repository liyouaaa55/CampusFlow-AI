"""v3.0 第四页展示层：AI 持续督办「风险摘要」的计数纪律。

本文件只验证展示层分组与计数，不涉及数据库写入、不改任何业务判定：
    1. 摘要分组必须覆盖 execution_check.SUPERVISION_TYPES 的全部类型，且不重不漏；
    2. 计数直接来自 supervision_items() 的结果，一个任务只计入一组（与 _classify 互斥一致）；
    3. 数量为 0 的分组仍然展示（避免"没有风险"被读成"没有这一项"）。
"""

from __future__ import annotations

from typing import Any

from tools import execution_check
from ui.pages import inspection_page as ip


def _item(kind: str, task_id: int) -> dict[str, Any]:
    """构造一条与 supervision_items() 同构的记录（字段名与值域保持一致）。"""
    return {
        "task_id": task_id,
        "kind": kind,
        "tag": execution_check.SUPERVISION_LABELS[kind],
        "problem": execution_check.SUPERVISION_LABELS[kind],
        "evidence": "测试依据",
        "suggestion": execution_check.SUPERVISION_SUGGESTIONS[kind],
        "tone": "danger" if kind == execution_check.SUPERVISION_OVERDUE else "warning",
        "title": f"任务{task_id}",
        "department": "测试部门",
        "owner": "测试负责人",
    }


def test_summary_groups_cover_all_supervision_types() -> None:
    """摘要分组必须恰好覆盖全部督办类型，不重不漏。"""
    covered = [kind for _label, _desc, _color, kinds in ip.SUMMARY_GROUPS for kind in kinds]
    assert sorted(covered) == sorted(execution_check.SUPERVISION_TYPES)
    assert len(covered) == len(set(covered)), "同一督办类型不得出现在两个分组里"


def test_summary_groups_have_label_and_description() -> None:
    """每个分组都必须有中文标签、说明与颜色，避免界面出现空卡片。"""
    for label, desc, color, _kinds in ip.SUMMARY_GROUPS:
        assert label and desc and color


def test_supervision_counts_is_mutually_exclusive_and_complete() -> None:
    """五个类型各一条时，各计数应为 1 且总数等于明细条数。"""
    items = [
        _item(execution_check.SUPERVISION_OVERDUE, 1),
        _item(execution_check.SUPERVISION_NOT_FEEDBACK, 2),
        _item(execution_check.SUPERVISION_DUE_SOON, 3),
        _item(execution_check.SUPERVISION_FEEDBACK_UNFINISHED, 4),
        _item(execution_check.SUPERVISION_STALE, 5),
    ]
    counts = ip._supervision_counts(items)

    assert set(counts) == {label for label, _d, _c, _k in ip.SUMMARY_GROUPS}
    assert sum(counts.values()) == len(items)
    for label, _desc, _color, kinds in ip.SUMMARY_GROUPS:
        assert counts[label] == len(kinds)


def test_supervision_counts_zero_groups_are_kept() -> None:
    """无关注事项时也保留全部分组（含 0），界面结构保持稳定。"""
    counts = ip._supervision_counts([])
    assert len(counts) == len(ip.SUMMARY_GROUPS)
    assert all(value == 0 for value in counts.values())


def test_supervision_counts_ignores_order_but_not_kind() -> None:
    """计数只看类型，与明细顺序无关。"""
    items = [
        _item(execution_check.SUPERVISION_DUE_SOON, 9),
        _item(execution_check.SUPERVISION_OVERDUE, 3),
    ]
    counts = ip._supervision_counts(items)
    labels = [label for label, _d, _c, _k in ip.SUMMARY_GROUPS]
    assert counts[labels[2]] == 1  # 逾期未完成
    assert counts[labels[1]] == 1  # 临期未完成
    assert sum(counts.values()) == 2
