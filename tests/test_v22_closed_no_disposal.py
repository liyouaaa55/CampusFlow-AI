"""正式闭环任务不再被误催办 / 再次执行（v2.2 收口）。

覆盖本问题直接涉及的三处保护：
    1. agent.execution_agent.analyze        —— 已闭环任务早退，不调用大模型、不写 analyze 日志；
    2. tools.execution_tool.execute          —— 已闭环任务拒绝执行（最终闸门）；
    3. ui.pages.dashboard_page.collect_findings —— 已闭环任务不出现在督办发现中。

以及必须守住的既有约束：
    4. 闭环前生成的旧方案，闭环后确认不得执行（也不得修改风险等级）；
    5. 未闭环任务的既有分析 / 执行流程不受影响；
    6. 黑名单与高风险授权门禁不变。

数据安全：
    - 全部使用 tmp_path 下的隔离数据库，绝不触碰 data/campusflow.db；
    - 核验一律走「人工裁决」分支（llm_fn=None），不调用任何真实大模型。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent import execution_agent
from database import closure_store, sqlite_db, task_metadata_store
from tools import closure_service
from tools import execution_tool as et
from tools import task_dispatch, verification_service
from ui.pages import dashboard_page, execution_panel

# 固定基准日：截止日早于它即判定为 overdue（trigger ESCALATE）
BASE_DATE = date(2026, 10, 5)
DEADLINE_OVERDUE = "2026-10-03"

CONTENT = "已提交整改台账与现场照片"

ANALYSIS: dict[str, Any] = {
    "situation": "任务已逾期且尚未完成。",
    "reasons": ["截止时间已早于当前日期。"],
}


class SpyLLM:
    """记录调用次数的假模型；不应被已闭环任务调用。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        self.calls += 1
        return ANALYSIS


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "closed_no_disposal.db"
    sqlite_db.init_db(path)
    return path


def _open_task(db: Path, **overrides: Any) -> int:
    """写入一条「逾期、低风险、进行中」任务（否则分析不会产出 ESCALATE）。"""
    payload: dict[str, Any] = {
        "title": "实验室安全隐患整改",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": DEADLINE_OVERDUE,
        "priority": "high",
        "status": "processing",
        "risk_level": "green",
        "need_confirm": False,
        "description": "整改隐患并提交台账",
    }
    payload.update(overrides)
    return int(sqlite_db.create_task(payload, db_path=db)["id"])


def _close(db: Path, task_id: int) -> None:
    """把任务推进到「正式闭环」（核验走人工裁决，全程零大模型调用）。"""
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交整改台账"],
        closure_source="manual",
        closure_confirmed=True,
        db_path=db,
    )
    task_dispatch.dispatch_task(task_id, db_path=db)
    task_dispatch.submit_work(task_id, CONTENT, db_path=db)
    verification_service.verify_submission(task_id, decision="approved", db_path=db)
    outcome = closure_service.close_task(task_id, closed_by="督办员", db_path=db)
    assert outcome["ok"] is True, outcome
    assert closure_store.is_closed(task_id, db_path=db) is True


# ============================================================
# 一、analyze：已闭环任务早退
# ============================================================


def test_closed_task_analyze_returns_none_without_llm_or_log(db: Path) -> None:
    """要求 1：已闭环任务 analyze 返回 None，零模型调用、零新日志。"""
    task_id = _open_task(db)
    _close(db, task_id)

    before = sqlite_db.get_logs(task_id, db_path=db)
    spy = SpyLLM()

    result = execution_agent.analyze(
        task_id, llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result is None
    assert spy.calls == 0
    assert sqlite_db.get_logs(task_id, db_path=db) == before


# ============================================================
# 二、execute：最终闸门拒绝
# ============================================================


def test_closed_task_execute_refused_and_risk_unchanged(db: Path) -> None:
    """要求 2：已闭环任务 execute 被拒绝，risk_level 不变，无成功执行记录。"""
    task_id = _open_task(db)
    _close(db, task_id)

    task = sqlite_db.get_task(task_id, db_path=db)
    before_logs = sqlite_db.get_logs(task_id, db_path=db)

    with pytest.raises(ValueError):
        et.execute(
            et.ACTION_ESCALATE,
            task=task,
            params={"risk_level": "red"},
            operator="督办员",
            db_path=db,
            today=BASE_DATE,
        )

    after = sqlite_db.get_task(task_id, db_path=db)
    assert after["risk_level"] == "green"

    logs = sqlite_db.get_logs(task_id, db_path=db)
    assert logs == before_logs
    assert all(log["action"] != et.ACTION_LOG_EVENT[et.ACTION_ESCALATE] for log in logs)


# ============================================================
# 三、驾驶舱：督办发现过滤
# ============================================================


def test_closed_task_absent_from_collect_findings(db: Path) -> None:
    """要求 3：已闭环任务不出现在 collect_findings 结果中。"""
    task_id = _open_task(db)

    # 闭环前：该逾期任务本应作为督办发现出现
    assert any(f["task_id"] == task_id for f in dashboard_page.collect_findings(db_path=db))

    _close(db, task_id)

    assert dashboard_page.collect_findings(db_path=db) == []


# ============================================================
# 四、陈旧方案：闭环后确认不得执行
# ============================================================


def test_plan_created_before_closure_cannot_execute_after_closure(db: Path) -> None:
    """要求 4：闭环前生成的旧方案，闭环后确认不得执行、不得改风险等级。"""
    task_id = _open_task(db)

    plan = execution_agent.analyze(
        task_id, llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert plan is not None
    assert plan["action"] == et.ACTION_ESCALATE

    _close(db, task_id)

    outcome = execution_panel.confirm_execution(
        plan, operator="督办员", db_path=db, today=BASE_DATE
    )

    assert outcome["ok"] is False
    after = sqlite_db.get_task(task_id, db_path=db)
    assert after["risk_level"] == "green"
    actions = [log["action"] for log in sqlite_db.get_logs(task_id, db_path=db)]
    assert et.ACTION_LOG_EVENT[et.ACTION_ESCALATE] not in actions


# ============================================================
# 五、回归：未闭环任务不受影响
# ============================================================


def test_open_task_still_analyzed_and_escalated(db: Path) -> None:
    """要求 5：未闭环任务仍能正常分析并执行允许的动作。"""
    task_id = _open_task(db)
    spy = SpyLLM()

    plan = execution_agent.analyze(
        task_id, llm_fn=spy, db_path=db, today=BASE_DATE
    )
    assert plan is not None
    assert plan["action"] == et.ACTION_ESCALATE
    assert spy.calls == 1

    task = sqlite_db.get_task(task_id, db_path=db)
    outcome = et.execute(
        et.ACTION_ESCALATE,
        task=task,
        params={"risk_level": "red"},
        operator="督办员",
        db_path=db,
        today=BASE_DATE,
    )
    assert outcome["applied"] is True
    assert sqlite_db.get_task(task_id, db_path=db)["risk_level"] == "red"


# ============================================================
# 六、既有门禁不变
# ============================================================


def test_blacklist_still_rejected(db: Path) -> None:
    """要求 6：黑名单动作仍被拒绝（未被本次改动放宽）。"""
    task_id = _open_task(db)
    task = sqlite_db.get_task(task_id, db_path=db)

    with pytest.raises(ValueError):
        et.execute(et.BLACKLIST_MARK_COMPLETED, task=task, db_path=db)


def test_high_risk_closure_still_requires_authorization(db: Path) -> None:
    """要求 6：高风险任务缺少显式授权仍无法闭环（闭环门禁未被放宽）。"""
    task_id = _open_task(db, risk_level="red")
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交整改台账"],
        closure_source="manual",
        closure_confirmed=True,
        db_path=db,
    )
    task_dispatch.dispatch_task(task_id, db_path=db)
    task_dispatch.submit_work(task_id, CONTENT, db_path=db)
    verification_service.verify_submission(task_id, decision="approved", db_path=db)

    denied = closure_service.close_task(task_id, closed_by="督办员", db_path=db)

    assert denied["ok"] is False
    assert "高风险" in denied["reason"]
    assert closure_store.is_closed(task_id, db_path=db) is False
