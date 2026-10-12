"""P0：核验—闭环状态一致性（按 submission_digest 绑定当前提交版本）。

覆盖：
    - 当前版本核验：重新提交后旧版本结论不再算数；
    - 阶段卡不被「旧版本通过」误导（与闭环口径一致）；
    - ready_close 才能闭环；未核验/未通过当前版本必须被拒绝；
    - 已下发任务不得重复下发；已闭环任务不得再下发。

全部使用 tmp_path 隔离数据库；页面函数用默认库，故 monkeypatch sqlite_db.DB_PATH。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from database import sqlite_db, task_metadata_store, verification_store
from tools import closure_service as closure_svc
from tools import task_dispatch
from tools import verification_service as verify_svc
from ui.pages import inspection_page


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "p0_consistency.db"
    sqlite_db.init_db(path)
    # 页面函数（_current_stage / _current_verification）不显式传 db_path，
    # 走 sqlite_db.DB_PATH；这里切到隔离库。
    monkeypatch.setattr(sqlite_db, "DB_PATH", path)
    return path


def _prepare(db: Path) -> int:
    """建任务 → 确认 → 设已确认办结标准 → 下发，返回 task_id。"""
    task = sqlite_db.create_task(
        {
            "title": "P0 状态一致性",
            "department": "教务处",
            "owner": "陈老师",
            "deadline": "2026-12-31",
            "priority": "medium",
            "status": "pending",
            "risk_level": "green",
            "need_confirm": True,
            "description": "完成教材征订统计并提交统计表。",
        },
        db_path=db,
    )
    task_id = int(task["id"])
    sqlite_db.update_status(task_id, "confirmed", operator="AI", db_path=db)
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交教材征订统计表"],
        closure_source="manual",
        closure_confirmed=True,
        db_path=db,
    )
    task_dispatch.dispatch_task(task_id, db_path=db)
    return task_id


def test_current_verification_binds_digest(db: Path) -> None:
    task_id = _prepare(db)
    task_dispatch.submit_work(task_id, "统计表 v1", db_path=db)
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)
    assert inspection_page._current_verification(task_id) is not None

    # 重新提交（新 digest）：旧核验不再属于当前版本
    task_dispatch.submit_work(task_id, "统计表 v2（修订）", db_path=db)
    assert inspection_page._current_verification(task_id) is None


def test_stage_not_fooled_by_stale_approval(db: Path) -> None:
    task_id = _prepare(db)
    task_dispatch.submit_work(task_id, "统计表 v1", db_path=db)
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)
    assert inspection_page._current_stage(task_id) == "ready_close"

    task_dispatch.submit_work(task_id, "统计表 v2（修订）", db_path=db)
    # 旧版本通过不能冒充当前版本
    assert inspection_page._current_stage(task_id) == "pending_verify"


def test_stage_matches_closure_gate(db: Path) -> None:
    """阶段 ready_close 时闭环应成功（口径一致）。"""
    task_id = _prepare(db)
    task_dispatch.submit_work(task_id, "统计表 v1", db_path=db)
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)

    assert inspection_page._current_stage(task_id) == "ready_close"
    result = closure_svc.close_task(task_id, closed_by="AI", db_path=db)
    assert result["ok"] is True
    assert inspection_page._current_stage(task_id) == "closed"


def test_new_version_blocks_closure(db: Path) -> None:
    """当前版本未核验时闭环必须被拒绝（与阶段卡一致）。"""
    task_id = _prepare(db)
    task_dispatch.submit_work(task_id, "统计表 v1", db_path=db)
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)
    task_dispatch.submit_work(task_id, "统计表 v2（修订）", db_path=db)

    assert inspection_page._current_stage(task_id) == "pending_verify"
    result = closure_svc.close_task(task_id, closed_by="AI", db_path=db)
    assert result["ok"] is False


def test_not_passed_version_cannot_close(db: Path) -> None:
    task_id = _prepare(db)
    task_dispatch.submit_work(task_id, "统计表 v1", db_path=db)
    verify_svc.verify_submission(
        task_id, decision="need_supplement", reason="材料不足", db_path=db
    )
    assert inspection_page._current_stage(task_id) == "not_passed"
    result = closure_svc.close_task(task_id, closed_by="AI", db_path=db)
    assert result["ok"] is False


def test_dispatched_task_not_redispatched(db: Path) -> None:
    """已下发任务再次下发为幂等：不新增日志、不产生第二条记录。"""
    task_id = _prepare(db)
    before = len(sqlite_db.get_logs(task_id, db_path=db))
    result = task_dispatch.dispatch_task(task_id, db_path=db)
    assert result["already"] is True
    assert len(sqlite_db.get_logs(task_id, db_path=db)) == before


def test_closed_task_cannot_redispatch(db: Path) -> None:
    task_id = _prepare(db)
    task_dispatch.submit_work(task_id, "统计表", db_path=db)
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)
    closure_svc.close_task(task_id, closed_by="AI", db_path=db)

    assert inspection_page._current_stage(task_id) == "closed"
    with pytest.raises(task_dispatch.NotReadyError):
        task_dispatch.dispatch_task(task_id, db_path=db)


def test_history_marks_current_version(db: Path) -> None:
    """核验历史中，只有当前提交版本的记录应被识别为当前版本。"""
    task_id = _prepare(db)
    task_dispatch.submit_work(task_id, "统计表 v1", db_path=db)
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)
    task_dispatch.submit_work(task_id, "统计表 v2", db_path=db)

    current = inspection_page._current_verification(task_id)
    records = verification_store.list_verifications(task_id, db_path=db)
    assert current is None  # v2 未核验
    assert len(records) == 1  # 仅 v1 的核验
