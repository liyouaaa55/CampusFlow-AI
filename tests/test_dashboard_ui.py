"""智能驾驶舱首页改造的 UI 测试（v2.0 P0）。

重点验证：数据真实、零硬编码、零 LLM 调用、空数据不崩。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from agent import reminder_agent
from llm import client
from database import models, sqlite_db
from tools import execution_tool
from ui import labels
from ui.pages import dashboard_page as dp


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "dash.db"
    sqlite_db.init_db(p)
    return p


def _seed(db_path: Path) -> int:
    created = sqlite_db.create_task(
        {
            "title": "宿舍消防隐患专项排查",
            "department": "保卫处",
            "owner": "",
            "deadline": "2026-10-01",
            "priority": "high",
            "status": "pending",
            "risk_level": "red",
        },
        db_path=db_path,
    )
    return int(created["id"])


# ============================================================
# 一、副标题体现 AI 主动能力
# ============================================================


def test_subtitle_states_ai_proactivity() -> None:
    """副标题必须表达「AI 主动发现问题」。"""
    assert dp.PAGE_TITLE == "智能驾驶舱"
    assert "AI" in dp.PAGE_SUBTITLE
    assert "主动" in dp.PAGE_SUBTITLE


# ============================================================
# 二、数据全部来自真实任务，无硬编码
# ============================================================


def test_findings_are_real_tasks(db: Path) -> None:
    """AI 主动发现必须来自真实任务，不得硬编码示例。"""
    _seed(db)
    findings = dp.collect_findings(db_path=db)

    assert len(findings) == 1
    titles = {f["title"] for f in findings}
    assert titles == {"宿舍消防隐患专项排查"}

    ids = {f["task_id"] for f in findings}
    assert ids <= {t["id"] for t in sqlite_db.list_tasks(db_path=db)}


def test_no_hardcoded_sample_titles() -> None:
    """源码中不得出现 UI 示例里的假任务名。"""
    source = inspect.getsource(dp)
    for fake in ("学生资助材料审核", "校园消防设施检查", "运动会场地验收"):
        assert fake not in source, f"发现硬编码示例数据：{fake}"


def test_no_hardcoded_counts() -> None:
    """统计数字必须来自计算，不得写死。"""
    findings_source = inspect.getsource(dp.collect_findings)
    for literal in ("= 58", "= 6", "发现 6 项"):
        assert literal not in findings_source


# ============================================================
# 三、零 LLM 调用
# ============================================================


def test_findings_do_not_call_llm(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首页数据采集绝不能调用大模型。

    只禁止模型调用；check_all / get_task / get_logs 是必须走的既有读接口。
    """
    _seed(db)

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("驾驶舱不应调用大模型")

    monkeypatch.setattr(client, "chat_json", boom)

    findings = dp.collect_findings(db_path=db)
    assert len(findings) == 1  # 真实数据照常产出，只是不触发任何 LLM


def test_advice_comes_from_execution_tool_rule(db: Path) -> None:
    """处理建议必须复用 ExecutionAgent 的同一套确定性规则。"""
    task_id = _seed(db)
    findings = dp.collect_findings(db_path=db)
    task = sqlite_db.get_task(task_id, db_path=db)

    expected = execution_tool.select_action("overdue", task, [])
    assert findings[0]["advice"] == labels.action_label(expected)


# ============================================================
# 四、空数据不崩溃
# ============================================================


def test_empty_database_renders_without_findings(db: Path) -> None:
    """任务为空时不得报错，且不产生任何发现项。"""
    assert dp.collect_findings(db_path=db) == []
    assert dp._count_risk("red", db_path=db) == 0
    assert dp._count_need_confirm(db_path=db) == 0


def test_no_normal_task_produces_findings(db: Path) -> None:
    """进度正常、无风险、无待补充的任务不进入发现列表。"""
    sqlite_db.create_task(
        {
            "title": "常规工作",
            "deadline": "2099-12-31",
            "risk_level": "green",
            "status": "pending",
        },
        db_path=db,
    )
    assert dp.collect_findings(db_path=db) == []


# ============================================================
# 五、状态标签与事实描述
# ============================================================


def test_all_tags_are_chinese() -> None:
    """状态标签必须为中文，不得出现内部英文枚举。"""
    for level in ("overdue", "urgent", "due_soon", "normal"):
        assert not labels.level_label(level).isascii()
    assert not labels.risk_label("red").isascii()


def test_situation_contains_only_verifiable_facts(db: Path) -> None:
    """当前情况只陈述可核验事实。"""
    _seed(db)
    finding = dp.collect_findings(db_path=db)[0]

    assert "逾期" in finding["situation"]          # 来自 deadline 的真实计算
    assert "负责人未指定" in finding["situation"]   # 来自 owner 为空的真实状态


def test_tone_follows_existing_semantics() -> None:
    """标签配色沿用既有语义：高风险/逾期为 danger。"""
    assert dp._tone_of("overdue", "green", False) == "danger"
    assert dp._tone_of("normal", "red", False) == "danger"
    assert dp._tone_of("urgent", "green", False) == "warning"
    assert dp._tone_of("normal", "green", True) == "warning"
    assert dp._tone_of("due_soon", "green", False) == "info"


def test_risk_tag_takes_precedence(db: Path) -> None:
    """风险等级为 red 时标签显示风险，而非时间等级。"""
    _seed(db)
    finding = dp.collect_findings(db_path=db)[0]
    assert finding["tag"] == labels.risk_label("red")


# ============================================================
# 六、跳转定位
# ============================================================


def test_focus_task_state_roundtrip() -> None:
    """首页跳转写入的任务 id 能被督办中心取出，且只取一次。"""
    from ui.components import state

    state.set_focus_task(58)
    assert state.pop_focus_task() == 58
    assert state.pop_focus_task() is None


def test_tasks_page_preselects_focus_task() -> None:
    """任务督办中心应消费首页写入的定位状态。"""
    from ui.pages import tasks_page

    source = inspect.getsource(tasks_page)
    assert "pop_focus_task" in source
    assert "focus_index" in source