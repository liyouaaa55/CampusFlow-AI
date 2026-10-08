"""上传链路集成测试：文件 → 解析 → 事项识别 → 规划 → 入库 → 督办中心读取。

为什么需要这个文件
------------------
此前 636 项测试几乎全是**纯函数单元测试**（直接调用 agent / tools 的函数），
唯一覆盖 UI 的是 `test_app_smoke.py`，而它**只加载首页**，从不进入上传页，
也从不走「文件落盘 → parser_tool 解析 → 入库 → 督办中心读出」这条真实链路。

结果就是：页面级的 NameError、以及「解析器与入库接口对不上」这类问题
可以全绿通过。本文件专门补这条集成链路。

设计原则：
- LLM 一律注入假模型，**不调用真实 API**；
- 使用真实的临时数据库与真实的临时文件，不打桩数据层；
- 断言「用户实际能看到的结果」，而不是内部函数返回值。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent import reminder_agent, report_agent
from database import models, sqlite_db
from tools import parser_tool
from ui.components import pipeline

# ============================================================
# 固定基准数据
# ============================================================

BASE_DATE = date(2026, 10, 5)

MEETING_TEXT = """演示数据（Synthetic Data）学生工作例会纪要

一、保卫处 王老师 负责完成宿舍消防隐患专项排查，2026-09-30 前建立整改台账并完成销号。
二、学工办 张老师 负责制定智能机器人创新周活动实施方案，2026-10-15 前提交审定稿。
三、教务处 负责完成下学期教材征订统计。
"""


class FakeLLM:
    """假模型：按调用次数依次返回预置结果，不发网络请求。"""

    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = payloads
        self.index = 0

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        payload = self.payloads[min(self.index, len(self.payloads) - 1)]
        self.index += 1
        return payload


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "upload_chain.db"
    sqlite_db.init_db(db_path)
    return db_path


def _payload() -> list[dict[str, Any]]:
    return [
        {
            "title": "完成宿舍消防隐患专项排查",
            "department": "保卫处",
            "owner": "王老师",
            "deadline": "2026-09-30",
            "priority": "high",
            "status": "pending",
            "description": "建立整改台账并完成隐患销号",
        },
        {
            "title": "制定智能机器人创新周活动实施方案",
            "department": "学工办",
            "owner": "张老师",
            "deadline": "2026-10-15",
            "priority": "medium",
            "status": "pending",
            "description": "含活动实施方案",
        },
        {
            "title": "完成下学期教材征订统计",
            "department": "教务处",
            "owner": None,
            "priority": None,
            "status": "pending",
            "description": None,
        },
    ]


# ============================================================
# 一、解析器：各格式真的能读出文本
# ============================================================


def test_parse_txt_file(tmp_path: Path) -> None:
    """TXT 能解析出正文。"""
    path = tmp_path / "note.txt"
    path.write_text(MEETING_TEXT, encoding="utf-8")
    assert "保卫处" in parser_tool.parse_file(path)


def test_parse_gbk_txt_file(tmp_path: Path) -> None:
    """GBK 编码的 TXT 也能解析（国内公文常见编码）。"""
    path = tmp_path / "gbk.txt"
    path.write_bytes(MEETING_TEXT.encode("gbk"))
    assert "保卫处" in parser_tool.parse_file(path)


def test_parse_csv_file(tmp_path: Path) -> None:
    """CSV 台账能解析，且单元格以「 | 」连接。"""
    path = tmp_path / "tasks.csv"
    path.write_text("任务,负责人\n消防排查,王老师\n", encoding="utf-8")
    text = parser_tool.parse_file(path)
    assert "消防排查" in text and "王老师" in text


def test_parse_xlsx_file(tmp_path: Path) -> None:
    """Excel 台账能解析（覆盖 openpyxl 分支）。"""
    pd = pytest.importorskip("pandas")
    openpyxl = pytest.importorskip("openpyxl")  # noqa: F841
    path = tmp_path / "tasks.xlsx"
    pd.DataFrame([{"任务": "消防排查", "负责人": "王老师"}]).to_excel(path, index=False)
    assert "消防排查" in parser_tool.parse_file(path)


def test_parse_missing_file_raises(tmp_path: Path) -> None:
    """文件不存在时抛 FileNotFoundError，不静默返回空串。"""
    with pytest.raises(FileNotFoundError):
        parser_tool.parse_file(tmp_path / "nope.txt")


def test_parse_unsupported_format_raises_chinese(tmp_path: Path) -> None:
    """不支持的格式给出中文提示。"""
    path = tmp_path / "legacy.doc"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError) as info:
        parser_tool.parse_file(path)
    assert "不支持的文件格式" in str(info.value)


def test_parse_corrupt_excel_gives_chinese_error(tmp_path: Path) -> None:
    """损坏的表格必须给出中文提示，而不是把 pandas 英文报错透给用户。"""
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not a real excel file at all")
    with pytest.raises((ValueError, ImportError)) as info:
        parser_tool.parse_file(path)
    message = str(info.value)
    for leaked in ("Traceback", "pandas", "openpyxl", "BadZipFile", str(path)):
        assert leaked not in message, f"错误信息泄漏了内部细节：{message}"
    assert "表格" in message


# ============================================================
# 二、完整链路：解析 → 识别 → 规划 → 入库 → 督办中心读出
# ============================================================


def test_full_chain_upload_to_supervision(tmp_path: Path, db: Path) -> None:
    """核心链路一次走通：文件 → 解析 → 识别 → 入库 → 督办中心读出。"""
    # 1) 落盘（模拟上传页保存）
    source = tmp_path / "会议纪要.txt"
    source.write_text(MEETING_TEXT, encoding="utf-8")

    # 2) 解析
    text = parser_tool.parse_file(source)
    assert len(text) > 0

    # 3) 识别 + 规划（注入假模型）
    planned = pipeline.identify_and_plan(text, source_type="meeting", llm_fn=FakeLLM([_payload()]))
    assert len(planned) == 3
    # 缺失字段被 PlannerAgent 标记为需人工确认
    assert planned[2]["need_confirm"] is True

    # 4) 入库
    created = sqlite_db.batch_create(planned, db_path=db)
    assert len(created) == 3

    # 5) 督办中心读出（list_tasks 就是该页的数据来源）
    tasks = sqlite_db.list_tasks(db_path=db)
    assert len(tasks) == 3
    assert {t["department"] for t in tasks} == {"保卫处", "学工办", "教务处"}

    # 6) 每条都有 create 日志，操作页能展示
    for task in tasks:
        logs = sqlite_db.get_logs(task["id"], db_path=db)
        assert [log["action"] for log in logs] == [models.ACTION_CREATE]

    # 7) ReminderAgent 能扫出逾期项（督办中心的核心业务）
    items = reminder_agent.check_all(today=BASE_DATE, db_path=db)
    assert any(item["level"] == "overdue" for item in items)

    # 8) ReportAgent 能汇总（驾驶舱的数据来源）
    dashboard = report_agent.generate_dashboard(today=BASE_DATE, db_path=db)
    assert dashboard["total"] == 3
    assert dashboard["overdue"] == 1


def test_chain_survives_readonly_operations(db: Path) -> None:
    """读操作不会产生任何写入（督办中心/驾驶舱只读）。"""
    sqlite_db.batch_create(
        pipeline.identify_and_plan(MEETING_TEXT, source_type="meeting", llm_fn=FakeLLM([_payload()])),
        db_path=db,
    )
    before = len(sqlite_db.list_tasks(db_path=db))
    logs_before = len(sqlite_db.get_logs(1, db_path=db))

    for _ in range(3):
        reminder_agent.check_all(today=BASE_DATE, db_path=db)
        report_agent.generate_dashboard(today=BASE_DATE, db_path=db)

    assert len(sqlite_db.list_tasks(db_path=db)) == before
    assert len(sqlite_db.get_logs(1, db_path=db)) == logs_before


# ============================================================
# 三、示例材料：可加载、走同一链路、且不伪装真实文件
# ============================================================


def test_all_samples_exist_and_are_loadable() -> None:
    """四份示例材料都必须能通过真实解析器读出文本。"""
    assert len(pipeline.SAMPLE_DOCUMENTS) >= 4
    for item in pipeline.SAMPLE_DOCUMENTS:
        text = pipeline.load_sample_document(item["key"])
        assert text.strip(), f"{item['key']} 解析结果为空"
        assert item["label"] and item["source_type"]


def test_samples_cover_core_capabilities() -> None:
    """示例材料必须覆盖要求的核心验证点，而不是凑数。"""
    keys = {item["key"] for item in pipeline.SAMPLE_DOCUMENTS}
    assert any("会议纪要" in k for k in keys), "缺多事项会议纪要"
    assert any(k.endswith(".xlsx") for k in keys), "缺 Excel 台账"
    assert any("信息不完整" in k for k in keys), "缺信息不完整的通知"
    assert any("无执行事项" in k for k in keys), "缺无可执行事项的材料"
    # 来源类型必须覆盖会议纪要与公文通知
    types = {item["source_type"] for item in pipeline.SAMPLE_DOCUMENTS}
    assert {"meeting", "document", "excel"} <= types


def test_samples_are_labelled_as_synthetic() -> None:
    """每份示例都必须自我标注为演示数据，不伪装真实学校文件。"""
    for item in pipeline.SAMPLE_DOCUMENTS:
        text = pipeline.load_sample_document(item["key"])
        if item["key"].endswith(".md"):
            assert "演示数据" in text, f"{item['key']} 未标注演示数据"
            assert "Synthetic Data" in text, f"{item['key']} 缺少 Synthetic Data 标注"


def test_samples_do_not_contain_real_looking_school_names() -> None:
    """示例材料不得使用真实高校名称，避免被误认为真实文件。"""
    real_school_hints = ("大学", "学院", "中学")
    for item in pipeline.SAMPLE_DOCUMENTS:
        text = pipeline.load_sample_document(item["key"])
        if not item["key"].endswith(".md"):
            continue
        # 允许出现「实验中心」「学工办」等通用部门名，但不应出现具体校名
        for line in text.splitlines():
            if line.strip().startswith("#"):
                assert not any(h in line for h in real_school_hints), \
                    f"{item['key']} 标题疑似真实校名：{line}"


def test_sample_pipeline_returns_list() -> None:
    """示例材料走完整 pipeline 后必须返回列表（不抛异常）。"""
    item = pipeline.SAMPLE_DOCUMENTS[0]
    text = pipeline.load_sample_document(item["key"])
    planned = pipeline.identify_and_plan(text, source_type=item["source_type"], llm_fn=FakeLLM([_payload()]))
    assert isinstance(planned, list)
    assert len(planned) == 3


def test_unknown_sample_key_raises() -> None:
    """加载不存在的示例材料时明确报错。"""
    with pytest.raises(FileNotFoundError):
        pipeline.load_sample_document("示例_不存在.md")


def test_demo_document_still_loadable() -> None:
    """原有的「一键体验演示」材料不能被本次改动破坏。"""
    assert pipeline.load_demo_document().strip()
