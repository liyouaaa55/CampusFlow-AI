"""知识库（knowledge/）接线测试。

覆盖三类内容：
1. organization/departments.json 的读取、渲染与错误处理；
2. templates/ 的 JSON 与文本模板读取；
3. 接线效果：TaskAgent 注入部门职责、ReportAgent 采用报告模板标题。

所有涉及自定义文件的用例一律走 tmp_path，并在用例间清空缓存，避免相互污染。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent import report_agent, task_agent
from config import settings
from knowledge import KnowledgeError
from knowledge import loader as knowledge_loader


@pytest.fixture(autouse=True)
def _clear_knowledge_cache() -> Any:
    """每个用例前后清空知识库缓存，保证测试相互隔离。"""
    knowledge_loader.clear_cache()
    yield
    knowledge_loader.clear_cache()


def write_departments(path: Path, departments: list[dict[str, Any]]) -> Path:
    """写入一份部门职责文件并返回路径。"""
    path.write_text(json.dumps({"departments": departments}, ensure_ascii=False), encoding="utf-8")
    return path


# ============================================================
# 一、部门职责文件读取
# ============================================================


def test_real_departments_file_exists() -> None:
    """仓库内置的部门职责文件必须存在且路径来自 settings。"""
    assert knowledge_loader.ORGANIZATION_PATH == settings.ORGANIZATION_DIR / "departments.json"
    assert knowledge_loader.ORGANIZATION_PATH.is_file()


def test_load_real_departments() -> None:
    """内置部门职责可正常读取，且字段结构完整。"""
    departments = knowledge_loader.load_departments()
    assert len(departments) >= 5

    names = [item["name"] for item in departments]
    # 与 Prompt 中示例的部门口径保持一致
    assert "学工办" in names
    assert "保卫处" in names
    assert "教务处" in names

    for item in departments:
        assert isinstance(item["aliases"], list)
        assert item["duties"], f"{item['name']} 未配置职责"


def test_load_departments_missing_file_returns_empty(tmp_path: Path) -> None:
    """文件缺失属于「无知识」，返回空列表而不是报错。"""
    assert knowledge_loader.load_departments(tmp_path / "not_exists.json") == []


def test_load_departments_broken_json_raises(tmp_path: Path) -> None:
    """JSON 损坏必须显式报错，不静默产出脏数据。"""
    broken = tmp_path / "departments.json"
    broken.write_text("{不是合法 JSON", encoding="utf-8")
    with pytest.raises(KnowledgeError):
        knowledge_loader.load_departments(broken)


def test_load_departments_requires_departments_array(tmp_path: Path) -> None:
    """顶层缺少 departments 数组时报错。"""
    bad = tmp_path / "departments.json"
    bad.write_text(json.dumps({"foo": []}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(KnowledgeError, match="departments"):
        knowledge_loader.load_departments(bad)


def test_load_departments_rejects_non_object_item(tmp_path: Path) -> None:
    """departments 元素不是对象时报错。"""
    bad = write_departments(tmp_path / "departments.json", ["学工办"])  # type: ignore[list-item]
    with pytest.raises(KnowledgeError):
        knowledge_loader.load_departments(bad)


def test_load_departments_requires_name(tmp_path: Path) -> None:
    """部门缺少 name 字段时报错。"""
    bad = write_departments(tmp_path / "departments.json", [{"aliases": [], "duties": []}])
    with pytest.raises(KnowledgeError, match="name"):
        knowledge_loader.load_departments(bad)


def test_load_departments_rejects_non_list_aliases(tmp_path: Path) -> None:
    """aliases / duties 必须是数组。"""
    bad = write_departments(
        tmp_path / "departments.json",
        [{"name": "学工办", "aliases": "学生处", "duties": []}],
    )
    with pytest.raises(KnowledgeError, match="aliases"):
        knowledge_loader.load_departments(bad)


def test_load_departments_uses_cache(tmp_path: Path) -> None:
    """同一路径第二次读取走缓存；force_reload 可强制重读。"""
    path = write_departments(
        tmp_path / "departments.json",
        [{"name": "学工办", "aliases": [], "duties": ["学生事务"]}],
    )
    assert knowledge_loader.load_departments(path)[0]["name"] == "学工办"

    # 磁盘内容改为「保卫处」，缓存未失效时仍返回旧值
    write_departments(path, [{"name": "保卫处", "aliases": [], "duties": ["治安"]}])
    assert knowledge_loader.load_departments(path)[0]["name"] == "学工办"

    # 强制重读后拿到新值
    assert knowledge_loader.load_departments(path, force_reload=True)[0]["name"] == "保卫处"


# ============================================================
# 二、部门职责渲染为 Prompt 知识块
# ============================================================


def test_organization_block_contains_names_aliases_duties() -> None:
    """知识块包含部门名、别名与职责，并声明「不得据此编造」。"""
    block = knowledge_loader.organization_block()

    assert knowledge_loader.ORGANIZATION_BLOCK_HEADER in block
    assert "- 保卫处" in block
    assert "学生工作办公室" in block  # 别名
    assert "消防安全检查与隐患整改" in block  # 职责
    assert "禁止依据本参考编造" in block


def test_organization_block_empty_for_empty_departments() -> None:
    """显式传入空列表时不生成知识块。"""
    assert knowledge_loader.organization_block(departments=[]) == ""


def test_organization_block_without_aliases() -> None:
    """无别名的部门也能正常渲染。"""
    block = knowledge_loader.organization_block(
        departments=[{"name": "实验室管理处", "aliases": [], "duties": ["设备管理"]}]
    )
    assert "- 实验室管理处" in block
    assert "别名" not in block
    assert "· 设备管理" in block


# ============================================================
# 三、模板读取
# ============================================================


def test_load_report_template_json() -> None:
    """内置报告模板可读取，且包含标题与章节定义。"""
    template = knowledge_loader.load_template_json(report_agent.REPORT_TEMPLATE_NAME)
    assert template["weekly_title"] == report_agent.WEEKLY_REPORT_TITLE
    assert set(report_agent.DEFAULT_SECTION_TITLES) <= set(template["sections"])


def test_load_template_json_missing_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模板文件缺失时返回空字典，调用方回退内置值。"""
    monkeypatch.setattr(settings, "TEMPLATES_DIR", tmp_path)
    assert knowledge_loader.load_template_json("report.json") == {}


def test_load_template_json_broken_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON 模板损坏时抛 KnowledgeError。"""
    (tmp_path / "report.json").write_text("{{{", encoding="utf-8")
    monkeypatch.setattr(settings, "TEMPLATES_DIR", tmp_path)
    with pytest.raises(KnowledgeError):
        knowledge_loader.load_template_json("report.json")


def test_load_template_text_markdown() -> None:
    """文本模板（会议纪要 / 任务模板）可直接读取全文。"""
    minutes = knowledge_loader.load_template_text("meeting_minutes.md")
    task_template = knowledge_loader.load_template_text("task.md")

    assert "# 会议纪要模板" in minutes
    assert "议定事项" in minutes
    assert "risk_level" in task_template


def test_load_template_text_missing_returns_empty() -> None:
    """文本模板缺失时返回空字符串。"""
    assert knowledge_loader.load_template_text("not_exists.md") == ""


def test_template_path_is_sanitized() -> None:
    """模板名称带路径时只取文件名，避免目录穿越。"""
    # 只取 basename，因此不会命中上级目录中的文件
    assert knowledge_loader.load_template_text("../../config/settings.py") == ""


# ============================================================
# 四、接线效果：TaskAgent 注入部门职责
# ============================================================


def _capture_llm(received: dict[str, str]) -> Any:
    """构造一个只记录 Prompt、返回空数组的假模型调用。"""

    def fake_llm(system_prompt: str, user_prompt: str) -> Any:
        received["system"] = system_prompt
        received["user"] = user_prompt
        return []

    return fake_llm


def test_task_agent_injects_organization_by_default() -> None:
    """默认行为：TaskAgent 会把部门职责注入 System Prompt。"""
    received: dict[str, str] = {}
    task_agent.extract_tasks("保卫处负责完成消防隐患排查。", llm_fn=_capture_llm(received))

    assert knowledge_loader.ORGANIZATION_BLOCK_HEADER in received["system"]
    assert "保卫处" in received["system"]
    # 基础约束仍然完整保留
    assert "只输出一个 JSON 数组" in received["system"]


def test_task_agent_can_disable_knowledge() -> None:
    """显式传 organization="" 时禁用知识库，回退基础 Prompt。"""
    received: dict[str, str] = {}
    task_agent.extract_tasks("保卫处负责完成消防隐患排查。", llm_fn=_capture_llm(received), organization="")

    assert knowledge_loader.ORGANIZATION_BLOCK_HEADER not in received["system"]
    assert received["system"] == task_agent.prompts.TASK_EXTRACTION_SYSTEM


def test_task_agent_accepts_custom_organization_block() -> None:
    """可注入自定义知识块，便于替换为本校版本。"""
    received: dict[str, str] = {}
    task_agent.extract_tasks("甲部门负责事项。", llm_fn=_capture_llm(received), organization="【部门职责参考】自定义块")
    assert "自定义块" in received["system"]


def test_task_agent_missing_knowledge_file_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """知识文件缺失时静默回退基础 Prompt，不影响识别主流程。"""
    monkeypatch.setattr(knowledge_loader, "ORGANIZATION_PATH", tmp_path / "not_exists.json")

    received: dict[str, str] = {}
    task_agent.extract_tasks("保卫处负责完成消防隐患排查。", llm_fn=_capture_llm(received))
    assert received["system"] == task_agent.prompts.TASK_EXTRACTION_SYSTEM


def test_build_task_extraction_system_keeps_base_when_empty() -> None:
    """Prompt 组装函数在无知识块时原样返回基础 Prompt。"""
    assert task_agent.prompts.build_task_extraction_system("") == task_agent.prompts.TASK_EXTRACTION_SYSTEM
    assert task_agent.prompts.build_task_extraction_system("   ") == task_agent.prompts.TASK_EXTRACTION_SYSTEM
    assert task_agent.prompts.build_task_extraction_system("补充块").endswith("补充块")


# ============================================================
# 五、接线效果：ReportAgent 采用报告模板
# ============================================================


def test_report_loader_falls_back_on_broken_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """模板损坏时 load_report_template 返回空字典，不抛错（报告仍可生成）。"""

    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise KnowledgeError("模拟损坏")

    monkeypatch.setattr(report_agent, "load_template_json", boom)
    assert report_agent.load_report_template() == {}


def test_section_titles_default_to_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    """无模板时章节标题为内置默认值。"""
    monkeypatch.setattr(report_agent, "load_report_template", lambda force_reload=False: {})
    assert report_agent._section_titles() == report_agent.DEFAULT_SECTION_TITLES


def test_section_titles_read_from_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """模板提供的章节标题优先，未提供的键回退默认值。"""
    monkeypatch.setattr(
        report_agent,
        "load_report_template",
        lambda force_reload=False: {"sections": {"overview": "第一部分 概览", "risk": "   "}},
    )
    titles = report_agent._section_titles()

    assert titles["overview"] == "第一部分 概览"
    assert titles["risk"] == report_agent.DEFAULT_SECTION_TITLES["risk"]  # 空白值回退
    assert titles["suggestions"] == report_agent.DEFAULT_SECTION_TITLES["suggestions"]


def test_weekly_report_uses_template_titles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """周报正文使用模板中的标题。"""
    monkeypatch.setattr(
        report_agent,
        "load_report_template",
        lambda force_reload=False: {
            "weekly_title": "自定义周报标题",
            "sections": {"overview": "一、整体情况", "risk": "三、风险清单"},
        },
    )
    report = report_agent.generate_weekly_report(db_path=tmp_path / "empty.db")

    assert "自定义周报标题" in report
    assert "## 一、整体情况" in report
    assert "## 三、风险清单" in report


def test_department_report_uses_template_title(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """部门报告标题同样取自模板（含 {department} 占位符）。"""
    monkeypatch.setattr(
        report_agent,
        "load_report_template",
        lambda force_reload=False: {"department_title": "{department} 专项执行报告"},
    )
    report = report_agent.generate_department_report("保卫处", db_path=tmp_path / "empty.db")
    assert "保卫处 专项执行报告" in report
