"""v2.2 任务办理要求与办结标准测试（S2）。

重点：旧任务兼容、AI 建议强制未确认、1:1 不重复、级联、TaskAgent 七字段不变。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent import task_agent
from database import models, sqlite_db
from database import task_metadata_store as ms
from ui import labels

SEVEN = ["title", "department", "owner", "deadline", "priority", "status", "description"]


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "s2.db"
    sqlite_db.init_db(p)
    return p


def _task(db_path: Path, title: str = "消防检查") -> int:
    created = sqlite_db.create_task(
        {"title": title, "owner": "张老师", "deadline": "2026-11-01"}, db_path=db_path)
    return int(created["id"])


# ============ 一、表结构 ============


def test_table_created(db: Path) -> None:
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert models.TABLE_TASK_METADATA in names


def test_columns_order(db: Path) -> None:
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute(
        f"PRAGMA table_info({models.TABLE_TASK_METADATA})").fetchall()]
    conn.close()
    assert cols == list(models.TASK_METADATA_ALL_COLUMNS)


def test_task_id_unique_index_exists(db: Path) -> None:
    conn = sqlite3.connect(db)
    idx = {r[1] for r in conn.execute(
        f"PRAGMA index_list({models.TABLE_TASK_METADATA})").fetchall()}
    conn.close()
    assert idx, "task_id 应建唯一索引以保证 1:1"


def test_tasks_table_untouched(db: Path) -> None:
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    conn.close()
    assert "metadata" not in cols
    assert cols == list(models.TASK_ALL_COLUMNS)

# ============ 二、旧任务兼容 ============


def test_legacy_task_returns_safe_defaults(db: Path) -> None:
    """没有 metadata 行的旧任务读取必须返回安全默认值。"""
    tid = _task(db)
    meta = ms.get_metadata(tid, db_path=db)
    assert meta["id"] is None
    assert meta["execution_requirements"] == []
    assert meta["closure_criteria"] == []
    assert meta["closure_source"] is None
    assert meta["closure_confirmed"] is False
    assert ms.has_metadata(tid, db_path=db) is False


def test_existing_task_chain_unaffected(db: Path) -> None:
    """加新表后既有任务查询链路完全不受影响。"""
    tid = _task(db)
    assert sqlite_db.get_task(tid, db_path=db)["title"] == "消防检查"
    assert len(sqlite_db.list_tasks(db_path=db)) == 1


# ============ 三、创建 / 读取 / 更新 ============


def test_create_read_update(db: Path) -> None:
    tid = _task(db)
    saved = ms.save_metadata(
        tid,
        execution_requirements=["完成现场检查", "建立台账"],
        closure_criteria=["已提交检查记录"],
        closure_source=models.CLOSURE_SOURCE_MATERIAL,
        closure_confirmed=True, db_path=db,
    )
    assert saved["id"] is not None
    assert saved["execution_requirements"] == ["完成现场检查", "建立台账"]
    assert saved["closure_criteria"] == ["已提交检查记录"]
    assert saved["closure_confirmed"] is True

    fetched = ms.get_metadata(tid, db_path=db)
    assert fetched["closure_criteria"] == ["已提交检查记录"]

    updated = ms.set_execution_requirements(tid, ["完成现场检查"], db_path=db)
    assert updated["execution_requirements"] == ["完成现场检查"]
    assert updated["closure_criteria"] == ["已提交检查记录"]
    assert updated["closure_source"] == "material"


def test_repeated_save_single_row(db: Path) -> None:
    """同一任务更新 metadata 不得产生重复记录。"""
    tid = _task(db)
    for i in range(4):
        ms.save_metadata(tid, closure_criteria=[f"第{i}条"],
                         closure_source=models.CLOSURE_SOURCE_RULE, db_path=db)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM task_metadata WHERE task_id = ?",
                     (tid,)).fetchone()[0]
    conn.close()
    assert n == 1
    assert ms.get_metadata(tid, db_path=db)["closure_criteria"] == ["第3条"]


# ============ 四、来源标记与确认机制 ============


def test_ai_source_forced_unconfirmed(db: Path) -> None:
    """AI 建议即使显式传 confirmed=True 也必须被压回 False。"""
    tid = _task(db)
    rec = ms.save_metadata(
        tid, closure_criteria=["AI 猜测的标准"],
        closure_source=models.CLOSURE_SOURCE_AI, closure_confirmed=True, db_path=db)
    assert rec["closure_source"] == "ai"
    assert rec["closure_confirmed"] is False


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (models.CLOSURE_SOURCE_MATERIAL, "材料明确"),
        (models.CLOSURE_SOURCE_RULE, "规则推导"),
        (models.CLOSURE_SOURCE_AI, "AI 建议"),
        (models.CLOSURE_SOURCE_MANUAL, "人工确认"),
    ],
)
def test_all_sources_labeled(db: Path, source: str, expected: str) -> None:
    tid = _task(db)
    rec = ms.save_metadata(tid, closure_criteria=["标准"],
                           closure_source=source, db_path=db)
    assert rec["closure_source"] == source
    assert labels.CLOSURE_SOURCE_LABELS[source] == expected


def test_manual_source_can_confirm(db: Path) -> None:
    tid = _task(db)
    rec = ms.set_closure_criteria(
        tid, ["最终办结标准"], closure_source=models.CLOSURE_SOURCE_MANUAL,
        closure_confirmed=True, db_path=db)
    assert rec["closure_source"] == "manual"
    assert rec["closure_confirmed"] is True


def test_invalid_source_rejected(db: Path) -> None:
    tid = _task(db)
    with pytest.raises(ms.TaskMetadataError):
        ms.save_metadata(tid, closure_criteria=["x"], closure_source="瞎写", db_path=db)

# ============ 五、损坏数据不导致链路崩溃 ============


def test_corrupt_json_does_not_crash(db: Path) -> None:
    tid = _task(db)
    ms.save_metadata(tid, closure_criteria=["正常"],
                     closure_source=models.CLOSURE_SOURCE_RULE, db_path=db)
    with sqlite_db.get_connection(db) as conn:
        conn.execute("UPDATE task_metadata SET closure_criteria = ? WHERE task_id = ?",
                     ("{坏掉的 JSON", tid))
    assert ms.get_metadata(tid, db_path=db)["closure_criteria"] == []
    assert sqlite_db.get_task(tid, db_path=db)["title"] == "消防检查"


def test_non_list_json_degrades(db: Path) -> None:
    tid = _task(db)
    ms.save_metadata(tid, closure_criteria=["x"],
                     closure_source=models.CLOSURE_SOURCE_RULE, db_path=db)
    with sqlite_db.get_connection(db) as conn:
        conn.execute("UPDATE task_metadata SET execution_requirements = ? WHERE task_id = ?",
                     ('"应该是数组"', tid))
    assert ms.get_metadata(tid, db_path=db)["execution_requirements"] == []


def test_store_has_no_llm_dependency() -> None:
    import inspect
    source = inspect.getsource(ms).lower()
    assert "llm" not in source
    assert "import client" not in source


# ============ 六、级联与不存在任务 ============


def test_delete_task_cascades_metadata(db: Path) -> None:
    tid = _task(db)
    ms.save_metadata(tid, closure_criteria=["标准"],
                     closure_source=models.CLOSURE_SOURCE_RULE, db_path=db)
    sqlite_db.delete_task(tid, db_path=db)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM task_metadata WHERE task_id = ?",
                     (tid,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_unknown_task_raises(db: Path) -> None:
    with pytest.raises(sqlite_db.TaskNotFoundError):
        ms.save_metadata(9999, closure_criteria=["x"],
                         closure_source=models.CLOSURE_SOURCE_RULE, db_path=db)


def test_delete_metadata(db: Path) -> None:
    tid = _task(db)
    ms.save_metadata(tid, closure_criteria=["标准"],
                     closure_source=models.CLOSURE_SOURCE_RULE, db_path=db)
    assert ms.delete_metadata(tid, db_path=db) is True
    assert ms.get_metadata(tid, db_path=db)["id"] is None


# ============ 七、契约保护 ============


def test_task_agent_seven_field_contract_unchanged() -> None:
    """S2 不得改动 TaskAgent 的七字段输出契约。"""
    payload = [{
        "title": "消防检查", "department": "保卫处", "owner": "张老师",
        "deadline": "2026-11-01", "priority": "high", "status": "pending",
        "description": "完成检查",
    }]
    tasks = task_agent.extract_tasks("保卫处 张老师 负责消防检查，2026-11-01 前完成。",
                                     llm_fn=lambda s, u: payload)
    assert list(tasks[0].keys()) == SEVEN
    assert "metadata" not in tasks[0]


def test_event_actions_still_unchanged() -> None:
    assert models.EVENT_ACTIONS == (
        "analyze", "draft", "request", "confirm", "reject", "escalate")
    assert models.CHAIN_ACTIONS[:2] == ("dispatch", "submit")


def test_s1_verification_store_still_works(db: Path) -> None:
    from database import submission_store as sstore
    from database import verification_store as vstore
    tid = _task(db)
    sid = int(sstore.save_submission(tid, content="已完成", db_path=db)["id"])
    vstore.append_verification(tid, sid, models.VERIFICATION_APPROVED, db_path=db)
    assert len(vstore.list_verifications(tid, db_path=db)) == 1


def test_v21_dispatch_submit_unchanged(db: Path) -> None:
    from tools import task_dispatch
    tid = _task(db)
    assert task_dispatch.dispatch_task(tid, db_path=db)["ok"] is True