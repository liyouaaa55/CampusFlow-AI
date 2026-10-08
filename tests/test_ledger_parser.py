"""台账确定性解析测试（v2.0 P0-D）：表头识别、逐行保护、状态映射、字段不猜。

全部为纯函数测试，不调用真实模型。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import task_agent
from tools import ledger_parser

LEDGER13 = (
    "学生事务重点工作推进台账\n"
    "序号 | 工作事项 | 责任单位 | 负责人 | 截止时间 | 当前状态 | 备注\n"
    "1 | 各班学生基础信息核对及系统线上修正 | 各二级学院学工办 | 各班辅导员 | 2026-10-12 | 进行中 | 重点核对手机号\n"
    "2 | 汇总全校学生信息异常数据，导出错误清单 | 学生工作部信息管理科 | 张老师 | 2026-10-16 | 未开始 | 完成分类标记\n"
    "3 | 收集学生信息，修改佐证材料 | 各二级学院学工办 |  | 2026-10-18 | 未开始 | 双渠道报送\n"
    "4 | 2026届毕业生就业信息初步核查与系统更新 | 各二级学院 | 李辅导员 | 2026-10-10 | 已完成 | 更新签约状态\n"
    "5 | 毕业生就业协议、录用证明等材料初审核验 | 各学院就业办公室 | 王老师 | 2026-10-17 | 进行中 | 核查真实性\n"
    "6 | 学院毕业生就业数据统计，报送就业统计表 | 各学院就业办公室 | 刘老师 | 2026-10-20 | 未开始 | 需分管领导签字\n"
    "7 | 全校就业数据汇总校验，编制秋季就业工作简报 | 校就业指导中心 | 赵老师 | 2026-10-25 | 未开始 | 需二级学院复核\n"
    "8 | 毕业班就业政策主题宣讲及毕业生答疑 | 各毕业班 | 各班辅导员 | 2026-10-30 | 未开始 | 覆盖档案转递\n"
    "9 | 2026秋季学生信息核查专项工作总结撰写 | 学生工作部 |  |  | 待启动 | 时间待定\n"
    "10 | 2026届毕业生就业相关纸质材料分类归档入库 | 校就业指导中心 | 陈老师 | 2026-11-05 | 未开始 | 移交档案室"
)


# ============================================================
# 一、表头识别
# ============================================================


def test_detect_header_on_standard_ledger() -> None:
    text = "序号 | 事项 | 部门 | 负责人 | 截止时间 | 状态\n1 | 甲 | 教务处 | 张三 | 2026-10-01 | 未开始\n2 | 乙 | 学工办 | 李四 | 2026-10-02 | 进行中"
    found = ledger_parser.detect_header(text.split("\n"))
    assert found is not None
    assert found[1]["title"] == 1
    assert found[1]["seq"] == 0


def test_detect_header_requires_title_column() -> None:
    text = "序号 | 部门 | 负责人 | 状态\n1 | 教务处 | 张三 | 未开始\n2 | 学工办 | 李四 | 进行中"
    assert ledger_parser.detect_header(text.split("\n")) is None


def test_detect_header_requires_min_rows() -> None:
    text = "序号 | 事项 | 部门 | 状态\n1 | 甲 | 教务处 | 未开始"
    assert ledger_parser.detect_header(text.split("\n")) is None


def test_prose_is_not_ledger() -> None:
    prose = "一、会议决定\n保卫处负责完成消防检查。\n二、其他\n宣传部门将适时报道。"
    assert ledger_parser.parse_ledger(prose) is None


# ============================================================
# 二、Test A：三态各自保留，一个都不能丢
# ============================================================


def test_test_a_all_three_rows_preserved() -> None:
    text = "序号 | 事项 | 状态\n1 | 任务A | 已完成\n2 | 任务B | 进行中\n3 | 任务C | 待办"
    rows = ledger_parser.parse_ledger(text)
    assert rows is not None
    assert len(rows) == 3
    assert [r["title"] for r in rows] == ["任务A", "任务B", "任务C"]
    assert [r["status"] for r in rows] == ["completed", "processing", "pending"]


def test_completed_row_is_never_dropped() -> None:
    text = "序号 | 事项 | 状态\n1 | 已完成的工作 | 已完成\n2 | 待办的工作 | 未开始"
    rows = ledger_parser.parse_ledger(text)
    assert rows is not None
    assert len(rows) == 2
    assert rows[0]["status"] == "completed"


# ============================================================
# 三、Test B：owner / deadline 为空仍保留，且为 None
# ============================================================


def test_test_b_empty_owner_and_deadline_kept_as_none() -> None:
    text = "序号 | 事项 | 部门 | 负责人 | 截止时间 | 状态\n1 | 任务甲 | 教务处 |  |  | 未开始\n2 | 任务乙 | 学工办 |  |  | 进行中"
    rows = ledger_parser.parse_ledger(text)
    assert rows is not None
    assert len(rows) == 2
    for row in rows:
        assert row["owner"] is None
        assert row["deadline"] is None
        assert row["department"] is not None


def test_empty_cells_do_not_shift_columns() -> None:
    text = "序号 | 事项 | 部门 | 负责人 | 截止时间 | 状态\n1 | 任务甲 | 教务处 | 张三 | 2026-10-01 | 未开始\n2 | 任务乙 | 学工办 |  |  | 进行中"
    rows = ledger_parser.parse_ledger(text)
    assert rows is not None
    assert rows[0]["owner"] == "张三"
    assert rows[0]["deadline"] == "2026-10-01"
    assert rows[1]["owner"] is None
    assert rows[1]["deadline"] is None
    assert rows[1]["status"] == "processing"


# ============================================================
# 四、Test C / D：真实材料结构
# ============================================================


def test_material13_all_10_rows() -> None:
    """材料13 结构：10 行必须全部保留。"""
    rows = ledger_parser.parse_ledger(LEDGER13)
    assert rows is not None
    assert len(rows) == 10


def test_material13_completed_row_preserved() -> None:
    """核心回归：该行在稳定性评测中连续 5 次全部丢失，必须保留且状态正确。"""
    rows = ledger_parser.parse_ledger(LEDGER13)
    assert rows is not None
    hit = [r for r in rows if "毕业生就业信息初步核查" in r["title"]]
    assert len(hit) == 1
    assert hit[0]["status"] == "completed"
    assert hit[0]["owner"] == "李辅导员"
    assert hit[0]["department"] == "各二级学院"


def test_material13_row_with_empty_owner_and_deadline_kept() -> None:
    """owner 与 deadline 均为空的行仍须保留，字段为 None。"""
    rows = ledger_parser.parse_ledger(LEDGER13)
    assert rows is not None
    hit = [r for r in rows if "专项工作总结撰写" in r["title"]]
    assert len(hit) == 1
    assert hit[0]["owner"] is None
    assert hit[0]["deadline"] is None
    assert hit[0]["status"] == "pending"


def test_material13_status_distribution() -> None:
    """材料13 状态分布：completed 1 / processing 2 / pending 7。"""
    rows = ledger_parser.parse_ledger(LEDGER13)
    assert rows is not None
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    assert counts == {"completed": 1, "processing": 2, "pending": 7}


def test_material13_fields_come_from_original_cells() -> None:
    """字段必须取自原始单元格，不得从标题推断。"""
    rows = ledger_parser.parse_ledger(LEDGER13)
    assert rows is not None
    assert rows[0]["owner"] == "各班辅导员"
    assert rows[0]["department"] == "各二级学院学工办"
    assert rows[0]["deadline"] == "2026-10-12"


# ============================================================
# 五、逐行保护
# ============================================================


def test_one_row_yields_exactly_one_task() -> None:
    text = "序号 | 事项 | 部门 | 状态\n1 | 统筹推进赛前筹备、赛程编排与器材采购三项工作 | 教务处 | 未开始\n2 | 另一事项 | 学工办 | 进行中"
    rows = ledger_parser.parse_ledger(text)
    assert rows is not None
    assert len(rows) == 2
    assert rows[0]["title"].startswith("统筹推进赛前筹备")


def test_adjacent_rows_are_not_merged() -> None:
    text = "序号 | 事项 | 部门 | 状态\n1 | 甲事项 | 教务处 | 未开始\n2 | 乙事项 | 学工办 | 进行中"
    rows = ledger_parser.parse_ledger(text)
    assert rows is not None
    assert len(rows) == 2


def test_stats_block_not_treated_as_task() -> None:
    text = "序号 | 事项 | 部门 | 状态\n1 | 甲事项 | 教务处 | 未开始\n2 | 乙事项 | 学工办 | 进行中\n任务统计\n任务总数 | 2 | 完成率\n已完成 | 1\n延期 | 1"
    rows = ledger_parser.parse_ledger(text)
    assert rows is not None
    assert len(rows) == 2
    titles = " ".join(r["title"] for r in rows)
    assert "任务总数" not in titles
    assert "任务统计" not in titles


# ============================================================
# 六、状态映射
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("已完成", "completed"), ("完成", "completed"), ("已结束", "completed"),
        ("进行中", "processing"), ("正在进行", "processing"), ("推进中", "processing"),
        ("整改中", "processing"),
        ("未开始", "pending"), ("待办", "pending"), ("未完成", "pending"),
        ("已取消", "cancelled"), ("终止", "cancelled"),
    ],
)
def test_status_mapping(raw: str, expected: str) -> None:
    assert ledger_parser.map_status(raw) == expected


def test_unknown_status_returns_none() -> None:
    assert ledger_parser.map_status("延期") is None
    assert ledger_parser.map_status("") is None
    assert ledger_parser.map_status(None) is None


def test_undone_not_misread_as_completed() -> None:
    assert ledger_parser.map_status("未完成") == "pending"


def test_priority_left_to_planner() -> None:
    """priority 不得由表格文本猜测，必须留给 PlannerAgent。"""
    rows = ledger_parser.parse_ledger(LEDGER13)
    assert rows is not None
    assert all(r["priority"] is None for r in rows)




class BoomLLM:
    """若被调用即说明没有走台账路径。"""

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        raise AssertionError("台账路径不应调用大模型")


def test_extract_tasks_uses_ledger_path_without_llm() -> None:
    """命中台账时不得调用大模型，且 10 行全部保留。"""
    tasks = task_agent.extract_tasks(LEDGER13, source_type="excel", llm_fn=BoomLLM())
    assert len(tasks) == 10
    assert sum(1 for t in tasks if t["status"] == "completed") == 1


def test_extract_tasks_completed_row_never_dropped() -> None:
    """completed 行经完整链路后仍必须存在。"""
    tasks = task_agent.extract_tasks(LEDGER13, source_type="excel", llm_fn=BoomLLM())
    hit = [t for t in tasks if "毕业生就业信息初步核查" in t["title"]]
    assert len(hit) == 1
    assert hit[0]["status"] == "completed"
    assert hit[0]["owner"] == "李辅导员"


def test_extract_tasks_falls_back_for_prose() -> None:
    """散文材料必须继续走大模型路径。"""
    calls = {"n": 0}

    def counting(system_prompt: str, user_prompt: str) -> Any:
        calls["n"] += 1
        return [{"title": "甲", "status": "pending"}]

    task_agent.extract_tasks("一、会议决定\n保卫处负责完成消防检查。", source_type="document", llm_fn=counting)
    assert calls["n"] == 1


def test_extract_tasks_is_deterministic_for_ledger() -> None:
    """同一份台账连续解析 5 次，结果必须完全一致（本次修复的核心目标）。"""
    results = [task_agent.extract_tasks(LEDGER13, source_type="excel", llm_fn=BoomLLM()) for _ in range(5)]
    baseline = [(t["title"], t["status"], t["owner"], t["deadline"]) for t in results[0]]
    assert len(baseline) == 10
    for other in results[1:]:
        assert [(t["title"], t["status"], t["owner"], t["deadline"]) for t in other] == baseline

# ============================================================
# 八、隐患整改台账：整改中 -> processing
# ============================================================

MATERIAL3 = (
    "序号 | 隐患问题 | 责任部门 | 责任人 | 整改期限 | 当前状态 | 风险说明 | 备注\n"
    "1 | 西校区消防通道被杂物堵塞 | 后勤管理处 | 王建国 | 2026-10-12 | 整改中 | 消防通道堵塞属重大火灾隐患 | 已下达整改单\n"
    "2 | 东实验室应急照明灯故障 | 后勤管理处 | 王建国 | 2026-10-15 | 整改中 | 影响应急疏散 | 已通知维保\n"
    "3 | 学生宿舍违规使用大功率电器 | 学生工作部 | 李强 | 2026-10-18 | 未开始 | 存在火灾风险 | 待联合检查\n"
    "4 | 实验楼危化品未专柜存放 | 实验中心 | 张敏 | 2026-10-10 | 已完成 | 已整改完毕 | 验收通过"
)


def test_material3_rectifying_maps_to_processing() -> None:
    """材料3 的「整改中」必须映射为 processing，不得落回 pending。"""
    rows = ledger_parser.parse_ledger(MATERIAL3)
    assert rows is not None
    assert len(rows) == 4
    statuses = [r["status"] for r in rows]
    assert statuses == ["processing", "processing", "pending", "completed"]


def test_material3_rectifying_not_treated_as_pending() -> None:
    """显式断言：整改中 != pending。"""
    assert ledger_parser.map_status("整改中") == "processing"
    assert ledger_parser.map_status("整改中") != "pending"


def test_material3_known_statuses_unchanged() -> None:
    """既有映射不得因本次修改而改变。"""
    assert ledger_parser.map_status("已完成") == "completed"
    assert ledger_parser.map_status("进行中") == "processing"
    assert ledger_parser.map_status("待办") == "pending"
    assert ledger_parser.map_status("已取消") == "cancelled"


def test_material3_unknown_status_still_falls_back() -> None:
    """未知状态仍走原有 fallback（返回 None，交 normalize 落回默认）。"""
    assert ledger_parser.map_status("延期") is None
    assert ledger_parser.map_status("待定") is None
    assert ledger_parser.map_status("") is None
    assert ledger_parser.map_status(None) is None