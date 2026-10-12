"""v3.0 第一阶段：下发前智能分析 + 任务信息修正。

覆盖本次改造的两个核心问题：
    1. **「智能分析经常显示没有必要分析」的根因**：
       旧 analyze() 受提醒等级门槛驱动，正常进度的新任务直接返回 None；
       新的 analyze_before_dispatch() 与提醒等级无关，任何待下发任务都会被检查。
    2. **「无需额外处理」与「分析未成功」必须区分**：
       模型失败抛 ExecutionAnalysisError，由调用方展示为「未成功」，
       绝不返回 None 被界面误读成「没有必要分析」。

数据安全：全部使用 tmp_path 隔离数据库，注入假模型，不触碰 data/campusflow.db。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent import execution_agent
from database import closure_store, models, sqlite_db, task_metadata_store
from llm import client
from tools import closure_service, task_dispatch
from tools import verification_service
from ui.pages import tasks_page

BASE_DATE = date(2026, 10, 5)
DEADLINE_NORMAL = "2026-10-20"      # 距基准日 15 天 -> normal（旧逻辑正是死在这里）
DEADLINE_OVERDUE = "2026-10-03"     # 距基准日 -2 天 -> overdue

CONTENT = "已提交整改台账"

READY_PAYLOAD: dict[str, Any] = {
    "readiness": "ready",
    "summary": "任务信息完整，可直接下发。",
    "issues": [],
}


class SpyLLM:
    """记录调用次数的假模型；可注入固定返回或固定异常。"""

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = READY_PAYLOAD if payload is None else payload
        self.error = error
        self.calls = 0

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "predispatch.db"
    sqlite_db.init_db(path)
    return path


def _seed(db: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "实验室安全隐患整改",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": DEADLINE_NORMAL,
        "priority": "high",
        "status": "processing",
        "risk_level": "green",
        "need_confirm": False,
        "description": "整改隐患并提交台账",
    }
    payload.update(overrides)
    return sqlite_db.create_task(payload, db_path=db)


def _close(db: Path, task_id: int) -> None:
    task_metadata_store.set_closure_criteria(
        task_id, ["提交整改台账"], closure_source="manual",
        closure_confirmed=True, db_path=db,
    )
    task_dispatch.dispatch_task(task_id, db_path=db)
    task_dispatch.submit_work(task_id, CONTENT, db_path=db)
    verification_service.verify_submission(task_id, decision="approved", db_path=db)
    outcome = closure_service.close_task(task_id, closed_by="督办员", db_path=db)
    assert outcome["ok"] is True, outcome


# ============================================================
# 一、根因回归：正常进度的任务也必须被分析
# ============================================================


def test_normal_level_task_is_still_analyzed(db: Path) -> None:
    """提醒等级为 normal（未临期）的任务同样会被检查——旧逻辑在此返回 None。"""
    task_id = int(_seed(db)["id"])
    spy = SpyLLM()

    result = execution_agent.analyze_before_dispatch(
        task_id, llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result["kind"] == "analysis"
    assert result["executed"] is True
    assert result["model_used"] is True
    assert result["level"] == "normal"        # 等级只是参考信息，不作门槛
    assert result["readiness"] == "ready"
    assert result["missing_fields"] == []
    assert result["blocking"] is False
    assert spy.calls == 1


def test_overdue_task_is_also_analyzed(db: Path) -> None:
    """逾期的任务同样走下发前检查（这两种情况共用同一入口，不互相排斥）。"""
    task_id = int(_seed(db, deadline=DEADLINE_OVERDUE)["id"])
    result = execution_agent.analyze_before_dispatch(
        task_id, llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert result["kind"] == "analysis"
    assert result["level"] == "overdue"


# ============================================================
# 二、规则层缺失项：一定被报出并阻断下发
# ============================================================


def test_missing_owner_blocks_and_is_reported(db: Path) -> None:
    """缺负责人：即便模型说 ready，也必须报出缺失项并判定暂不可下发。"""
    task_id = int(_seed(db, owner=None)["id"])

    result = execution_agent.analyze_before_dispatch(
        task_id, llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert "owner" in result["missing_fields"]
    assert result["blocking"] is True
    owner_issues = [i for i in result["issues"] if i["field"] == "owner"]
    assert owner_issues, "规则层缺失项必须出现在问题清单里"
    assert owner_issues[0]["severity"] == "high"


def test_missing_deadline_and_requirement_reported(db: Path) -> None:
    task_id = int(_seed(db, deadline=None, description=None)["id"])
    result = execution_agent.analyze_before_dispatch(
        task_id, llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert {"deadline", "description"} <= set(result["missing_fields"])
    assert result["blocking"] is True


# ============================================================
# 三、失败与「无需处理」严格区分
# ============================================================


def test_model_failure_raises_analysis_error(db: Path) -> None:
    """模型失败必须抛错，而不是返回 None（否则会被误读成「无需分析」）。"""
    task_id = int(_seed(db)["id"])
    spy = SpyLLM(error=client.LLMError("模拟限流"))

    with pytest.raises(execution_agent.ExecutionAnalysisError):
        execution_agent.analyze_before_dispatch(
            task_id, llm_fn=spy, db_path=db, today=BASE_DATE
        )
    assert spy.calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"readiness": "maybe", "summary": "x", "issues": []},
        {"readiness": "ready", "summary": "", "issues": []},
        {"readiness": "ready", "summary": "x", "issues": "not-a-list"},
        {"readiness": "ready", "summary": "x", "issues": [{"problem": ""}]},
    ],
)
def test_invalid_model_payload_raises(payload: Any, db: Path) -> None:
    """模型返回结构非法一律抛错，绝不落成一条「看起来正常」的结论。"""
    task_id = int(_seed(db)["id"])
    with pytest.raises(execution_agent.ExecutionAnalysisError):
        execution_agent.analyze_before_dispatch(
            task_id, llm_fn=SpyLLM(payload), db_path=db, today=BASE_DATE
        )


def test_run_predispatch_analysis_maps_failure_to_error_kind(db: Path) -> None:
    """页面层把失败映射为 kind="error"，与「未发现问题」明确区分。"""
    task_id = int(_seed(db)["id"])
    outcome = tasks_page.run_predispatch_analysis(
        task_id, llm_fn=SpyLLM(error=client.LLMError("网络异常")), db_path=db, today=BASE_DATE
    )
    assert outcome["ok"] is False
    assert outcome["kind"] == "error"
    assert "未成功" in outcome["message"]

    success = tasks_page.run_predispatch_analysis(
        task_id, llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert success["ok"] is True
    assert success["kind"] == "analysis"
    assert success["result"]["readiness"] == "ready"


# ============================================================
# 四、已闭环任务：不分析
# ============================================================


def test_closed_task_skips_model(db: Path) -> None:
    task_id = int(_seed(db)["id"])
    _close(db, task_id)
    spy = SpyLLM()

    result = execution_agent.analyze_before_dispatch(
        task_id, llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result["kind"] == "closed"
    assert result["executed"] is False
    assert spy.calls == 0
    assert closure_store.is_closed(task_id, db_path=db) is True


# ============================================================
# 五、任务信息修正
# ============================================================


def test_save_task_info_updates_db_and_writes_log(db: Path) -> None:
    task_id = int(_seed(db, owner=None, deadline=None)["id"])

    outcome = tasks_page.save_task_info(
        task_id,
        title="实验室安全隐患整改（修订）",
        department="保卫处",
        owner="王老师",
        deadline="2026-11-01",
        priority="high",
        description="补齐执行要求",
        operator="李主任",
        db_path=db,
    )
    assert outcome["ok"] is True

    task = sqlite_db.get_task(task_id, db_path=db)
    assert task["owner"] == "王老师"
    assert task["deadline"] == "2026-11-01"
    assert task["title"] == "实验室安全隐患整改（修订）"
    # 修正必须留痕（action 为人工确认事件），且不改动任务状态
    actions = [log["action"] for log in sqlite_db.get_logs(task_id, db_path=db)]
    assert models.ACTION_CONFIRM in actions
    assert task["status"] == "processing"


def test_save_task_info_rejects_invalid_input(db: Path) -> None:
    """标题被清空 / 截止时间格式非法时给出中文原因，不写入脏数据。"""
    task_id = int(_seed(db)["id"])

    empty_title = tasks_page.save_task_info(
        task_id, title="", department="保卫处", owner="王老师",
        deadline="2026-11-01", priority="high", description="x", db_path=db,
    )
    assert empty_title["ok"] is False

    bad_deadline = tasks_page.save_task_info(
        task_id, title="任务", department="保卫处", owner="王老师",
        deadline="下周三", priority="high", description="x", db_path=db,
    )
    assert bad_deadline["ok"] is False

    # 未被写入：仍是原值
    assert sqlite_db.get_task(task_id, db_path=db)["title"] == "实验室安全隐患整改"


def test_update_task_fields_rejects_unknown_field(db: Path) -> None:
    task_id = int(_seed(db)["id"])
    with pytest.raises(ValueError):
        sqlite_db.update_task_fields(task_id, {"status": "completed"}, db_path=db)
