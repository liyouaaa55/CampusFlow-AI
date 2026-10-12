"""v2.2 智能处置分析能力恢复测试。

核心业务边界：**分析结论与动作校验解耦**。
    - 智能分析是只读过程，生成「情况说明 + 判断依据」；
    - 某个具体动作是否允许执行，由规则系统决定；
    - 动作被规则限制时，分析结论仍必须保留并展示，不能退化为一句通用提示。

全部注入假模型，使用 tmp_path 独立数据库，不触碰真实 data/campusflow.db。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent import execution_agent
from database import models, sqlite_db, submission_store, task_metadata_store, verification_store
from llm import client
from tools import execution_tool as et
from ui.pages import execution_panel

BASE_DATE = date(2026, 10, 5)
DEADLINE_DUE_SOON = "2026-10-07"
DEADLINE_OVERDUE = "2026-10-03"
DEADLINE_NORMAL = "2026-10-20"


class SpyLLM:
    """记录调用次数与 Prompt 的假模型。"""

    def __init__(self, payload: Any = None) -> None:
        self.payload = payload or {
            "situation": "该事项临近截止，尚未提交办理结果。",
            "reasons": ["截止时间为 2026-10-07。", "历史中未见回执提交记录。"],
        }
        self.calls = 0
        self.user_prompt = ""

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        self.calls += 1
        self.user_prompt = user_prompt
        return self.payload


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "v22_analysis.db"
    sqlite_db.init_db(db_path)
    return db_path


def seed(db_path: Path, **over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "实验室安全检查",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": DEADLINE_DUE_SOON,
        "priority": "high",
        "status": "pending",
        "risk_level": "green",
        "description": "排查实验室安全隐患",
    }
    payload.update(over)
    created = sqlite_db.create_task(payload, db_path=db_path)
    return sqlite_db.get_task(created["id"], db_path=db_path)  # type: ignore[index]


# ============================================================
# 一、正常分析：产出有实际内容的结论
# ============================================================


def test_analysis_returns_real_content(db: Path) -> None:
    task = seed(db)
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is True
    assert spy.calls == 1
    assert outcome["plan"]["situation"] == spy.payload["situation"]
    assert outcome["plan"]["reasons"] == spy.payload["reasons"]
    assert outcome["plan"]["action"] in et.ALLOWED_ACTIONS


# ============================================================
# 二、核心：动作被规则限制时，分析结论仍保留
# ============================================================


def test_blocked_action_still_returns_analysis(db: Path) -> None:
    """构造近期 draft 事件触发 L6 重复处置拦截，分析结论必须仍在。"""
    task = seed(db, deadline=DEADLINE_DUE_SOON)
    sqlite_db.append_log(
        task["id"],
        models.ACTION_DRAFT,
        operator="张老师",
        remark="[DRAFT_NOTIFY] level=due_soon days=2 | 上次已提醒",
        db_path=db,
    )
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert outcome["kind"] == "validator_rejected"
    assert spy.calls == 1
    plan = outcome["plan"]
    # 分析结论未被丢弃
    assert plan["situation"] == spy.payload["situation"]
    assert plan["reasons"] == spy.payload["reasons"]
    # 动作被标记为受限，并带上原因
    assert plan["blocked"] is True
    assert plan["block_reason"]
    assert plan["action"] == et.ACTION_DRAFT_NOTIFY


# ============================================================
# 三、失败反馈分类：不同情况不同提示，不统一成「条件不满足」
# ============================================================


def test_llm_error_reported_as_analysis_error(db: Path) -> None:
    task = seed(db)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise client.LLMError("模拟超时")

    outcome = execution_panel.run_analysis(task["id"], llm_fn=boom, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert outcome["kind"] == "analysis_error"
    assert outcome["message"] == execution_panel.MSG_ANALYSIS_FAILED
    assert outcome["plan"] is None


def test_normal_task_reports_no_action(db: Path) -> None:
    task = seed(db, deadline=DEADLINE_NORMAL)
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert outcome["kind"] == "no_action"
    assert spy.calls == 0


def test_empty_history_still_analyzes(db: Path) -> None:
    """历史只有 create 事件时仍能正常分析（不回退成泛化提示）。"""
    task = seed(db, deadline=DEADLINE_OVERDUE, risk_level="green")
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is True
    assert spy.calls == 1
    assert outcome["plan"]["situation"]


# ============================================================
# 四、上下文扩展：办结标准与办理进度进入分析
# ============================================================


def test_prompt_includes_criteria_and_progress(db: Path) -> None:
    task = seed(db)
    task_metadata_store.set_closure_criteria(
        task["id"],
        ["完成全部检查项"],
        closure_source="manual",
        closure_confirmed=True,
        db_path=db,
    )
    submission_store.save_submission(task_id=task["id"], content="已完成初检", db_path=db)
    spy = SpyLLM()

    execution_agent.analyze(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    prompt = spy.user_prompt
    assert "办结标准与办理要求" in prompt
    assert "办理进度（回执与核验）" in prompt
    assert "完成全部检查项" in prompt
    assert "已完成初检" in prompt


def test_context_placeholders_when_missing(db: Path) -> None:
    task = seed(db)

    assert (
        execution_agent._render_criteria_context(task["id"], db)
        == execution_agent.NO_CRITERIA_TEXT
    )
    assert (
        execution_agent._render_progress_context(task["id"], db)
        == execution_agent.NO_SUBMISSION_TEXT
    )


def test_progress_context_reflects_verification(db: Path) -> None:
    task = seed(db)
    submission = submission_store.save_submission(
        task_id=task["id"], content="已完成", db_path=db
    )
    verification_store.append_verification(
        task_id=task["id"],
        submission_id=int(submission["id"]),
        status=models.VERIFICATION_NEED_SUPPLEMENT,
        reason="缺少签字确认表",
        db_path=db,
    )

    rendered = execution_agent._render_progress_context(task["id"], db)

    assert "need_supplement" in rendered
    assert "缺少签字确认表" in rendered
