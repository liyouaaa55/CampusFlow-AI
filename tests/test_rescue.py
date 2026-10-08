"""Evidence Rescue 确定性规则单元测试（v2.0 Final Regression）。

只测 rescue 的确定性规则，不测 LLM 行为。
"""

from __future__ import annotations

from tools import rescue

# 材料8 原文关键片段（含成文日期，用于日期年份取证）
SOURCE = (
    "XX学院2026年秋季田径运动会筹备与宣传工作进展情况（2026年10月6日）。"
    "场地设施方面，跑道划线已完工，主席台搭建及音响、照明系统检修正在实施，"
    "检录区、候赛区等功能分区已划定。"
    "物资保障方面，竞赛器材到货七成，奖牌奖品与应急物资采购同步推进。"
    "主题海报、道旗、横幅进入制作阶段，校内电子屏每日轮播不少于6小时。"
    "赛场无线网络扩容因设备到货延迟，完成时间调整为11月11日，未影响总体安排。"
    "当前筹备工作已进入攻坚阶段，四条线同步推进，整体进度符合预期。"
    "下一步将抓好场地联合验收、秩序册编印、志愿者培训、开闭幕式彩排等环节。"
)


def titles(cands):
    return [c["title"] for c in cands]


# ============================================================
# 1~3：真实在办事项被保留，且不因并列名词被拆分
# ============================================================


def test_chairwork_kept_as_one_combined_task():
    """用例1：主席台搭建及音响、照明系统检修正在实施 -> 一个组合任务。"""
    cands = rescue.find_candidates(SOURCE)
    hit = [c for c in cands if "主席台搭建" in c["title"]]
    assert len(hit) == 1
    assert hit[0]["title"] == "主席台搭建及音响、照明系统检修"
    assert hit[0]["status"] == "processing"


def test_supply_purchase_kept():
    """用例2：奖牌奖品与应急物资采购同步推进 -> 保留一个任务。"""
    cands = rescue.find_candidates(SOURCE)
    hit = [c for c in cands if "奖牌奖品" in c["title"]]
    assert len(hit) == 1
    assert hit[0]["title"] == "奖牌奖品与应急物资采购"


def test_poster_combined_not_split():
    """用例3：主题海报、道旗、横幅进入制作阶段 -> 保留为一个组合任务。"""
    cands = rescue.find_candidates(SOURCE)
    hit = [c for c in cands if "海报" in c["title"]]
    assert len(hit) == 1
    assert hit[0]["title"] == "主题海报、道旗、横幅制作"


def test_parallel_nouns_are_not_split():
    """并列名词不得被拆成多个任务。"""
    cands = rescue.find_candidates(SOURCE)
    poster = [c for c in cands if "海报" in c["title"]]
    assert len(poster) == 1
    assert "道旗" in poster[0]["title"]
    assert "横幅" in poster[0]["title"]

# ============================================================
# 4：日期解析（必须是系统标准 YYYY-MM-DD）
# ============================================================


def test_adjusted_deadline_parsed_to_system_format():
    """用例4：完成时间调整为11月11日 -> deadline 解析为标准 YYYY-MM-DD。"""
    cands = rescue.find_candidates(SOURCE)
    hit = [c for c in cands if "无线网络扩容" in c["title"]]
    assert len(hit) == 1
    assert hit[0]["deadline"] == "2026-11-11"


def test_deadline_format_matches_task_agent_standard():
    """Rescue 产出的 deadline 必须能通过 TaskAgent 的标准正则。"""
    import re
    from agent import task_agent

    cands = rescue.find_candidates(SOURCE)
    for c in cands:
        if c["deadline"] is not None:
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", c["deadline"]), c["deadline"]
            assert task_agent._normalize_deadline(c["deadline"]) == c["deadline"]


def test_deadline_none_without_year_evidence():
    """原文无年份证据时不得推断年份，deadline 应为 None。"""
    text = "赛场无线网络扩容因设备到货延迟，完成时间调整为11月11日。"
    cands = rescue.find_candidates(text)
    assert len(cands) == 1
    assert cands[0]["deadline"] is None


# ============================================================
# 5~7：不得触发补回的情形
# ============================================================


def test_completed_work_not_rescued():
    """用例5：跑道划线已完工 -> 不生成新任务。"""
    cands = rescue.find_candidates(SOURCE)
    assert "跑道划线已完工" not in titles(cands)
    assert not [c for c in cands if "跑道划线" in c["title"]]


def test_vague_object_not_rescued():
    """用例6：四条线同步推进 -> 不生成任务（对象过于笼统）。"""
    cands = rescue.find_candidates(SOURCE)
    assert not [c for c in cands if "四条线" in c["title"]]


def test_directional_sentence_not_rescued():
    """用例7：方向性表述不生成任何 Rescue 任务。"""
    cands = rescue.find_candidates(SOURCE)
    for key in ("场地联合验收", "秩序册", "志愿者培训", "开闭幕式彩排"):
        assert not [c for c in cands if key in c["title"]], key


def test_directional_not_rescued_with_extra_leading_verb():
    """回归：LLM 给方向性事项加了原文中没有的前置动词时，仍须被剔除。

    实际泄漏样本：「筹备开闭幕式彩排」——「筹备」是模型加的，原文没有。
    若 title_core 未剥离该动词，全字符匹配会失败并被误保留。
    """
    cands = rescue.find_candidates(SOURCE)
    for key in ("场地联合验收", "秩序册", "志愿者培训", "开闭幕式彩排"):
        assert not [c for c in cands if key in c["title"]], key


def test_directional_guard_strips_added_leading_verbs():
    """Directional Guard 必须能剥离模型自行添加的前置动词。"""
    from tools import directional_guard

    for title in ("筹备开闭幕式彩排", "开闭幕式彩排", "开展场地联合验收", "编印秩序册"):
        verdict = directional_guard.classify(title, SOURCE)
        assert verdict["directional"] is True, title


def test_real_task_still_kept_with_verb_stripping():
    """剥离动词后，真实任务不得被误判为方向性。"""
    from tools import directional_guard

    simple = directional_guard.classify("完成消防设施检查", "后勤处负责完成消防设施检查。")
    assert simple["directional"] is False
    other = directional_guard.classify("组织实验室安全检查", "下周组织一次实验室安全检查。")
    assert other["directional"] is False


def test_completed_markers_never_trigger():
    """已完成类表达一律不触发补回。"""
    text = "扫尾工作已结束，方案已确定，队伍已组建，预案已通过，预算已批复。"
    assert rescue.find_candidates(text) == []

# ============================================================
# 8~9：责任字段必须有当前句证据
# ============================================================


def test_department_none_when_absent_in_sentence():
    """用例8：当前句没有部门 -> department=None（不得跨句继承）。"""
    cands = rescue.find_candidates(SOURCE)
    for c in cands:
        assert c["department"] is None
        assert c["owner"] is None


def test_no_cross_sentence_inheritance():
    """前文出现过的部门不得被自动赋给当前句的事项。"""
    text = (
        "后勤保障处与体育教学部联合开展场地验收准备。"
        "奖牌奖品与应急物资采购同步推进。"
    )
    cands = rescue.find_candidates(text)
    assert len(cands) == 1
    assert cands[0]["department"] is None
    assert cands[0]["owner"] is None


def test_priority_left_to_planner():
    """priority 不由本层处理，固定为 None。"""
    for c in rescue.find_candidates(SOURCE):
        assert c["priority"] is None


# ============================================================
# 10：判重
# ============================================================


def test_merge_does_not_duplicate_existing():
    """用例10：LLM 已有相同任务时，Rescue 不重复添加。"""
    cands = rescue.find_candidates(SOURCE)
    existing = [{"title": c["title"], "status": "processing"} for c in cands]
    merged = rescue.merge(existing, cands)
    assert len(merged) == len(cands)


def test_merge_tolerates_llm_rephrasing():
    """LLM 换了措辞但字符高度重叠时，同样不重复添加。"""
    cands = rescue.find_candidates(SOURCE)
    existing = [{"title": "主席台搭建及音响照明系统检修", "status": "processing"}]
    merged = rescue.merge(existing, cands)
    # LLM 那条改写措辞的主席台应与候选合并，只保留一条
    assert len(merged) == len(cands)
    chair = [t for t in merged if "主席台" in t["title"]]
    assert len(chair) == 1


def test_merge_adds_when_missing():
    """LLM 漏掉时 Rescue 必须补上。"""
    cands = rescue.find_candidates(SOURCE)
    merged = rescue.merge([], cands)
    assert len(merged) == 4
    assert all(t["status"] == "processing" for t in merged)


def test_merge_into_empty_list_runs_rescue():
    """回归：LLM 返回空时，补救层仍须执行（此前被提前 return 跳过）。"""
    from agent import task_agent
    from tools import parser_tool

    r = task_agent.extract_tasks(
        SOURCE, source_type="text", llm_fn=lambda s, u: []
    )
    assert len(r) == 4