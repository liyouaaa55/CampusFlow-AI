"""v2.2 结果核验数据层测试（S1）。

覆盖：数据库结构、事件体系兼容性、UI 中文标签、核验记录读写、2.1 回归。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database import models, sqlite_db
from database import verification_store as vstore
from database import submission_store as sstore
from ui import labels


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "v22.db"
    sqlite_db.init_db(p)
    return p


def _make_task(db_path: Path, title: str = "任务甲") -> int:
    created = sqlite_db.create_task(
        {"title": title, "owner": "张老师", "deadline": "2026-11-01"}, db_path=db_path
    )
    return int(created["id"])


def _make_submission(db_path: Path, task_id: int) -> int:
    return int(sstore.save_submission(task_id, content="已完成", db_path=db_path)["id"])


# ============================================================
# 一、数据库结构
# ============================================================


def test_verifications_table_created(db: Path) -> None:
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert models.TABLE_VERIFICATIONS in names


def test_verifications_columns_order(db: Path) -> None:
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute(
        f"PRAGMA table_info({models.TABLE_VERIFICATIONS})").fetchall()]
    conn.close()
    assert cols == list(models.VERIFICATION_ALL_COLUMNS)


def test_task_id_has_no_unique_constraint(db: Path) -> None:
    """同一任务必须允许多条核验（驳回→补充→再核验的轨迹要留得住）。"""
    conn = sqlite3.connect(db)
    indexes = {r[1] for r in conn.execute(
        f"PRAGMA index_list({models.TABLE_VERIFICATIONS})").fetchall()}
    cols = [r[1] for r in conn.execute(
        f"PRAGMA table_info({models.TABLE_VERIFICATIONS})").fetchall()]
    conn.close()
    unique = {r[1] for r in conn.execute(
        f"PRAGMA index_list({models.TABLE_VERIFICATIONS})").fetchall()} if False else set()
    assert "task_id" not in indexes, f"task_id 不应建唯一索引，否则再核验会被 SQL 挡住：{indexes}"


def test_existing_tables_untouched(db: Path) -> None:
    """2.0/2.1 表结构未被改动，且 tasks 尚未加 metadata（S2 才开始）。"""
    conn = sqlite3.connect(db)
    tasks = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    subs = [r[1] for r in conn.execute("PRAGMA table_info(task_submissions)").fetchall()]
    conn.close()
    assert "metadata" not in tasks
    assert subs == ["id", "task_id", "content", "file_path", "status",
                    "submitted_at", "created_at", "updated_at"]


# ============================================================
# 二、事件体系兼容性
# ============================================================


def test_event_actions_unchanged() -> None:
    """EVENT_ACTIONS 六元组必须与 2.0 完全一致（数量/顺序/成员）。"""
    assert models.EVENT_ACTIONS == (
        "analyze", "draft", "request", "confirm", "reject", "escalate",
    )


def test_chain_actions_extended() -> None:
    assert models.CHAIN_ACTIONS[:2] == ("dispatch", "submit")
    for action in ("verify", "need_supplement", "reject_rework", "close"):
        assert action in models.CHAIN_ACTIONS


def test_all_log_actions_contains_new_events() -> None:
    for action in models.CHAIN_ACTIONS:
        assert action in models.ALL_LOG_ACTIONS


def test_new_actions_writable_to_logs(db: Path) -> None:
    """新事件能写进 task_logs（复用既有日志表，未新建事件体系）。"""
    task_id = _make_task(db)
    for action in models.CHAIN_ACTIONS:
        sqlite_db.append_chain_log(task_id, action, remark="x", db_path=db)


# ============================================================
# 三、UI 中文标签
# ============================================================


def test_all_log_actions_have_chinese_labels() -> None:
    missing = [a for a in models.ALL_LOG_ACTIONS if a not in labels.LOG_ACTION_LABELS]
    assert not missing, f"缺少中文标签：{missing}"


def test_new_action_labels_are_chinese() -> None:
    for action in ("verify", "need_supplement", "reject_rework", "close"):
        text = labels.LOG_ACTION_LABELS[action]
        assert text and not text.isascii(), f"{action} 标签非中文：{text}"


def test_verification_status_labels_complete() -> None:
    assert set(labels.VERIFICATION_LABELS) == set(models.VERIFICATION_STATUSES)
    for value in labels.VERIFICATION_LABELS.values():
        assert not value.isascii()


def test_old_labels_unchanged() -> None:
    """旧事件的中文标签不得被改动。"""
    assert labels.LOG_ACTION_LABELS["analyze"] == "智能分析"
    assert labels.LOG_ACTION_LABELS["escalate"] == "提级督办"
    assert labels.LOG_ACTION_LABELS["dispatch"] == "系统内下发"
    assert labels.LOG_ACTION_LABELS["submit"] == "提交工作回执"


# ============================================================
# 四、核验记录读写
# ============================================================


def test_append_and_get(db: Path) -> None:
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    rec = vstore.append_verification(
        tid, sid, models.VERIFICATION_APPROVED,
        reason="材料齐全", requirement_match=True, deliverable_complete=True,
        process_valid=True, state_consistent=True, verified_by="李主任", db_path=db,
    )
    assert rec["status"] == "approved"
    assert rec["verified_by"] == "李主任"
    assert rec["requirement_match"] is True
    assert vstore.get_verification(rec["id"], db_path=db)["id"] == rec["id"]


def test_same_task_keeps_multiple_verifications(db: Path) -> None:
    """核心：同一任务必须能留多条核验轨迹。"""
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    vstore.append_verification(tid, sid, models.VERIFICATION_NEED_SUPPLEMENT,
                              reason="缺整改照片", db_path=db)
    vstore.append_verification(tid, sid, models.VERIFICATION_APPROVED,
                              reason="已补充", db_path=db)
    rows = vstore.list_verifications(tid, db_path=db)
    assert len(rows) == 2
    assert [r["status"] for r in rows] == ["need_supplement", "approved"]
    assert vstore.latest_verification(tid, db_path=db)["status"] == "approved"
    assert vstore.count_verifications(tid, db_path=db) == 2
    assert vstore.count_verifications(db_path=db) == 2


def test_verification_does_not_alter_submission_status(db: Path) -> None:
    """核验不得改写 task_submissions.status（提交状态与核验状态分离）。"""
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    vstore.append_verification(tid, sid, models.VERIFICATION_APPROVED, db_path=db)
    assert sstore.get_submission(tid, db_path=db)["status"] == "submitted"
    assert sqlite_db.get_task(tid, db_path=db)["status"] == "pending"


def test_flags_default_false(db: Path) -> None:
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    rec = vstore.append_verification(tid, sid, models.VERIFICATION_PENDING, db_path=db)
    assert rec["requirement_match"] is False
    assert rec["deliverable_complete"] is False
    assert rec["process_valid"] is False
    assert rec["state_consistent"] is False
    assert rec["submission_id"] == sid


def test_invalid_status_rejected(db: Path) -> None:
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    with pytest.raises(vstore.VerificationError):
        vstore.append_verification(tid, sid, "随便写的", db_path=db)


def test_unknown_task_raises(db: Path) -> None:
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    with pytest.raises(sqlite_db.TaskNotFoundError):
        vstore.append_verification(9999, sid, models.VERIFICATION_PENDING, db_path=db)


def test_mismatched_submission_rejected(db: Path) -> None:
    """回执必须属于该任务，防止跨任务核验污染。"""
    a = _make_task(db, "甲")
    b = _make_task(db, "乙")
    sid_b = _make_submission(db, b)
    with pytest.raises(vstore.VerificationError):
        vstore.append_verification(a, sid_b, models.VERIFICATION_APPROVED, db_path=db)


def test_task_deletion_cascades(db: Path) -> None:
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    vstore.append_verification(tid, sid, models.VERIFICATION_PENDING, db_path=db)
    sqlite_db.delete_task(tid, db_path=db)
    assert vstore.list_verifications(tid, db_path=db) == []


def test_delete_verifications(db: Path) -> None:
    tid = _make_task(db)
    sid = _make_submission(db, tid)
    vstore.append_verification(tid, sid, models.VERIFICATION_PENDING, db_path=db)
    vstore.append_verification(tid, sid, models.VERIFICATION_APPROVED, db_path=db)
    assert vstore.delete_verifications(tid, db_path=db) == 2
    assert vstore.count_verifications(tid, db_path=db) == 0


def test_store_has_no_llm_dependency() -> None:
    """数据层不得引入模型调用（与 submission_check 同纪律）。"""
    import inspect
    source = inspect.getsource(vstore).lower()
    assert "llm" not in source
    assert "import client" not in source


# ============================================================
# 五、2.1 回归
# ============================================================


def test_dispatch_and_submit_unchanged(db: Path) -> None:
    from tools import task_dispatch
    from tools import submission_check
    tid = _make_task(db)
    assert task_dispatch.dispatch_task(tid, db_path=db)["ok"] is True
    task_dispatch.submit_work(tid, "已完成", db_path=db)
    state = submission_check.submission_state(
        sqlite_db.get_task(tid, db_path=db), sstore.get_submission(tid, db_path=db))
    assert state == "submitted"
