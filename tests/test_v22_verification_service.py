"""v2.2 Verification Service 测试（S3）。

重点：规则优先、AI 安全降级、不越权、核验历史保留、状态不被擅改。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from database import models, sqlite_db, submission_store
from database import task_metadata_store as ms
from database import verification_store as vstore
from tools import task_dispatch
from tools import verification_service as vs


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "s3.db"
    sqlite_db.init_db(p)
    return p


def _task(db_path: Path, title: str = "消防检查", risk: str = "green") -> int:
    return int(sqlite_db.create_task(
        {"title": title, "owner": "张老师", "deadline": "2026-11-01",
         "risk_level": risk}, db_path=db_path)["id"])


def _ready(db_path: Path, risk: str = "green") -> int:
    """构造一个可核验的任务：已下发、已提交、材料来源标准已确认、内容含证据。"""
    tid = _task(db_path, risk=risk)
    task_dispatch.dispatch_task(tid, db_path=db_path)
    task_dispatch.submit_work(tid, "已提交检查记录", db_path=db_path)
    ms.set_closure_criteria(tid, ["提交检查记录"], closure_source=models.CLOSURE_SOURCE_MATERIAL,
                            closure_confirmed=True, db_path=db_path)
    return tid


def _ai_ok(system: str, user: str) -> dict:
    return {"requirement_match": True, "confidence": 0.9, "evidence": "内容含检查记录"}


# ============ 一、无提交不能通过 ============


def test_no_submission_cannot_approve(db: Path) -> None:
    tid = _task(db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["ok"] is False
    assert result["status"] == models.VERIFICATION_PENDING
    assert result["recorded"] is False
    assert vstore.list_verifications(tid, db_path=db) == []


def test_approved_requires_submission(db: Path) -> None:
    """即使 AI 说满足，无提交也绝不放行。"""
    tid = _task(db)
    ms.set_closure_criteria(tid, ["标准"], closure_source=models.CLOSURE_SOURCE_MATERIAL,
                            closure_confirmed=True, db_path=db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["status"] != models.VERIFICATION_APPROVED
    assert result["status"] == models.VERIFICATION_PENDING

# ============ 二、标准缺失 / 未确认不能自动通过 ============


def test_no_criteria_goes_manual(db: Path) -> None:
    tid = _task(db)
    task_dispatch.dispatch_task(tid, db_path=db)
    task_dispatch.submit_work(tid, "已完成", db_path=db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["status"] == models.VERIFICATION_MANUAL_REVIEW
    assert result["manual_review_required"] is True


def test_ai_sourced_criteria_cannot_auto_approve(db: Path) -> None:
    """AI 来源标准（数据层强制未确认）不得自动通过。"""
    tid = _task(db)
    task_dispatch.dispatch_task(tid, db_path=db)
    task_dispatch.submit_work(tid, "已完成", db_path=db)
    ms.set_closure_criteria(tid, ["标准"], closure_source=models.CLOSURE_SOURCE_AI, db_path=db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["status"] == models.VERIFICATION_MANUAL_REVIEW


def test_confirmed_material_criteria_can_approve(db: Path) -> None:
    """材料明确且已确认的标准 + 证据齐备 + 过程合规 → 可通过。"""
    tid = _ready(db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["status"] == models.VERIFICATION_APPROVED
    assert result["requirement_match"] is True
    assert result["deliverable_complete"] is True
    assert result["process_valid"] is True


def test_missing_deliverable_goes_need_supplement(db: Path) -> None:
    """正文里找不到任何办结标准对应内容 → 需补充。"""
    tid = _task(db)
    task_dispatch.dispatch_task(tid, db_path=db)
    task_dispatch.submit_work(tid, "已完成", db_path=db)
    ms.set_closure_criteria(tid, ["提交整改照片"], closure_source=models.CLOSURE_SOURCE_MATERIAL,
                            closure_confirmed=True, db_path=db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["status"] == models.VERIFICATION_NEED_SUPPLEMENT
    assert result["deliverable_complete"] is False


# ============ 三、AI 安全降级 ============


@pytest.mark.parametrize(
    "bad_llm",
    [
        lambda s, u: (_ for _ in ()).throw(RuntimeError("timeout")),
        lambda s, u: (_ for _ in ()).throw(ValueError("bad json")),
        lambda s, u: "不是对象",
        lambda s, u: {"requirement_match": True, "confidence": 0.1},
        lambda s, u: {"requirement_match": True, "confidence": "很高"},
    ],
    ids=["timeout", "error", "not-dict", "low-confidence", "bad-confidence"],
)
def test_ai_failure_degrades_to_manual(db: Path, bad_llm) -> None:
    """AI 超时/非法/低置信一律降级为人工复核，绝不默认通过。"""
    tid = _ready(db)
    result = vs.verify_submission(tid, llm_fn=bad_llm, db_path=db)
    assert result["status"] == models.VERIFICATION_MANUAL_REVIEW
    assert result["recorded"] is True


def test_no_llm_degrades_to_manual(db: Path) -> None:
    """未启用 AI 时，语义维度不可判定 → 人工复核。"""
    tid = _ready(db)
    result = vs.verify_submission(tid, db_path=db)
    assert result["status"] == models.VERIFICATION_MANUAL_REVIEW


# ============ 四、高风险与过程不合规 ============


def test_high_risk_requires_manual(db: Path) -> None:
    tid = _ready(db, risk="red")
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["status"] == models.VERIFICATION_MANUAL_REVIEW


def test_submission_without_dispatch_fails_process(db: Path) -> None:
    """绕过下发直接提交 → 过程不合规。"""
    tid = _task(db)
    task_dispatch.submit_work(tid, "已提交检查记录", db_path=db)
    ms.set_closure_criteria(tid, ["提交检查记录"], closure_source=models.CLOSURE_SOURCE_MATERIAL,
                            closure_confirmed=True, db_path=db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["process_valid"] is False
    assert result["status"] != models.VERIFICATION_APPROVED

# ============ 五、历史保留 ============


def test_repeated_verification_keeps_history(db: Path) -> None:
    """同一任务多次核验必须保留全部历史，不得覆盖。"""
    tid = _ready(db)
    vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    ms.set_closure_criteria(tid, ["提交检查记录"], closure_source=models.CLOSURE_SOURCE_MATERIAL,
                            closure_confirmed=True, db_path=db)
    vs.verify_submission(tid, decision=models.VERIFICATION_NEED_SUPPLEMENT,
                         reason="材料待补", db_path=db)
    vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    rows = vstore.list_verifications(tid, db_path=db)
    assert len(rows) == 3
    assert rows[-1]["status"] == models.VERIFICATION_APPROVED


def test_manual_decision_recorded(db: Path) -> None:
    tid = _ready(db)
    result = vs.verify_submission(tid, decision=models.VERIFICATION_REJECTED,
                                 reason="照片不清晰", db_path=db)
    assert result["status"] == models.VERIFICATION_REJECTED
    assert result["reason"] == "照片不清晰"


# ============ 六、不越权 ============


def test_verification_does_not_touch_task_status(db: Path) -> None:
    """核验不得擅自改变 tasks.status（闭环动作不在本轮范围）。"""
    tid = _ready(db)
    before = sqlite_db.get_task(tid, db_path=db)["status"]
    vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert sqlite_db.get_task(tid, db_path=db)["status"] == before


def test_verification_does_not_touch_submission_status(db: Path) -> None:
    """核验不得改写 task_submissions.status。"""
    tid = _ready(db)
    vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert submission_store.get_submission(tid, db_path=db)["status"] == "submitted"


def test_no_close_event_written(db: Path) -> None:
    """核验通过不得写 close 事件——闭环动作不在本轮范围。"""
    tid = _ready(db)
    result = vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert result["status"] == models.VERIFICATION_APPROVED
    assert result["closure_performed"] is False
    assert models.ACTION_CLOSE not in [x["action"] for x in sqlite_db.get_logs(tid, db_path=db)]


def test_chain_log_written(db: Path) -> None:
    tid = _ready(db)
    vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    actions = [x["action"] for x in sqlite_db.get_logs(tid, db_path=db)]
    assert models.ACTION_VERIFY in actions


def test_cross_task_isolation(db: Path) -> None:
    """A 任务的核验不得污染 B 任务。"""
    a = _ready(db)
    b = _task(db, title="别的任务")
    vs.verify_submission(a, llm_fn=_ai_ok, db_path=db)
    assert vstore.list_verifications(a, db_path=db)
    assert vstore.list_verifications(b, db_path=db) == []


def test_unknown_task_raises(db: Path) -> None:
    with pytest.raises(sqlite_db.TaskNotFoundError):
        vs.verify_submission(9999, db_path=db)


def test_illegal_decision_rejected(db: Path) -> None:
    tid = _ready(db)
    with pytest.raises(vstore.VerificationError):
        vs.verify_submission(tid, decision="随便写的", db_path=db)


def test_collect_facts_does_not_write(db: Path) -> None:
    tid = _ready(db)
    before = len(sqlite_db.get_logs(tid, db_path=db))
    vs.collect_facts(tid, db_path=db)
    assert len(sqlite_db.get_logs(tid, db_path=db)) == before

# ============ 七、既有能力保护 ============


def test_event_actions_untouched() -> None:
    assert models.EVENT_ACTIONS == (
        "analyze", "draft", "request", "confirm", "reject", "escalate")


def test_chain_actions_semantics_preserved() -> None:
    assert models.CHAIN_ACTIONS[:2] == ("dispatch", "submit")
    for a in ("verify", "need_supplement", "reject_rework", "close"):
        assert a in models.CHAIN_ACTIONS


def test_v21_dispatch_submit_still_work(db: Path) -> None:
    tid = _task(db)
    assert task_dispatch.dispatch_task(tid, db_path=db)["ok"] is True
    task_dispatch.submit_work(tid, "已完成", db_path=db)
    assert submission_store.get_submission(tid, db_path=db)["status"] == "submitted"


def test_submission_check_untouched() -> None:
    """submission_check 必须仍是纯事实投影，不含核验逻辑。"""
    import inspect
    from tools import submission_check
    source = inspect.getsource(submission_check)
    assert "llm" not in source.lower()
    assert "requirement_match" not in source
    assert "closure" not in source


def test_service_does_not_replace_submission_check(db: Path) -> None:
    from tools import submission_check
    tid = _ready(db)
    vs.verify_submission(tid, llm_fn=_ai_ok, db_path=db)
    assert submission_check.submission_state(
        sqlite_db.get_task(tid, db_path=db),
        submission_store.get_submission(tid, db_path=db)) == "submitted"


def test_s2_metadata_store_still_works(db: Path) -> None:
    tid = _task(db)
    rec = ms.set_execution_requirements(tid, ["完成检查"], db_path=db)
    assert rec["execution_requirements"] == ["完成检查"]
    assert ms.get_metadata(tid, db_path=db)["id"] is not None


def test_s1_verification_store_still_works(db: Path) -> None:
    tid = _ready(db)
    sid = submission_store.get_submission(tid, db_path=db)["id"]
    vstore.append_verification(tid, sid, models.VERIFICATION_PENDING, db_path=db)
    assert len(vstore.list_verifications(tid, db_path=db)) == 1