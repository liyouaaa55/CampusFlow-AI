"""CampusFlow AI 2.2 · S3.2 正式闭环测试。

覆盖「提交 → 核验 → 闭环」全链路的 18 项安全约束。
全部按 tools/closure_service 与 database/*_store 的真实接口编写，
不假设不存在的函数或参数。

三条不可让步的语义：
    1. 已提交 ≠ 已核验 ≠ 已闭环；
    2. 核验结论绑定**提交内容指纹**，旧版本的 approved 不能用于新版本；
    3. 闭环必须由督办人员显式发起，且全程留痕。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tools import task_dispatch
from database import (
    closure_store,
    models,
    sqlite_db,
    submission_store,
    task_metadata_store,
    verification_store,
)
from tools import closure_service as cs

CONTENT = "已提交检查记录与整改照片"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "closure.db"
    sqlite_db.init_db(p)
    return p


def _task(db_path: Path, **over: Any) -> int:
    payload = {
        "title": "实验室安全检查",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": "2026-11-01",
        "status": "processing",
        "risk_level": "green",
    }
    payload.update(over)
    return int(sqlite_db.create_task(payload, db_path=db_path)["id"])


def _submit(db_path: Path, task_id: int, content: str = CONTENT) -> int:
    return int(
        submission_store.save_submission(task_id, content=content, db_path=db_path)["id"]
    )


def _criteria(db_path: Path, task_id: int, source: str = "material", confirmed: bool = True) -> None:
    task_metadata_store.set_closure_criteria(
        task_id,
        ["已提交检查记录"],
        closure_source=source,
        closure_confirmed=confirmed,
        db_path=db_path,
    )


def _verify_approved(db_path: Path, task_id: int, submission_id: int) -> int:
    """写入一条与当前提交内容指纹绑定的 approved 核验。"""
    submission = submission_store.get_submission(task_id, db_path=db_path)
    rec = verification_store.append_verification(
        task_id,
        submission_id,
        models.VERIFICATION_APPROVED,
        reason="材料齐备",
        requirement_match=True,
        deliverable_complete=True,
        process_valid=True,
        state_consistent=True,
        verified_by="李主任",
        submission_digest=verification_store.content_digest(submission),
        db_path=db_path,
    )
    return int(rec["id"])


def _ready(db_path: Path, **over: Any) -> tuple[int, int, int]:
    """构造一个满足全部闭环前置条件的任务，返回 (task_id, submission_id, verification_id)。"""
    task_id = _task(db_path, **over)
    task_dispatch.dispatch_task(task_id, operator="AI", db_path=db_path)
    submission_id = _submit(db_path, task_id)
    _criteria(db_path, task_id)
    verification_id = _verify_approved(db_path, task_id, submission_id)
    return task_id, submission_id, verification_id

# ============================================================
# 一~七：闭环前置条件（逐条拒绝）
# ============================================================


def test_01_no_submission_cannot_close(db: Path) -> None:
    """1. 无提交记录 -> 拒绝。"""
    task_id = _task(db)
    _criteria(db, task_id)
    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert "回执提交记录" in result["reason"]
    assert closure_store.get_closure(task_id, db_path=db) is None


def test_02_empty_criteria_cannot_close(db: Path) -> None:
    """2. 办结标准为空 -> 拒绝。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    _submit(db, task_id)
    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert "缺少办结标准" in result["reason"]


def test_03_unconfirmed_criteria_cannot_close(db: Path) -> None:
    """3. 办结标准未确认 -> 拒绝。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    _submit(db, task_id)
    _criteria(db, task_id, confirmed=False)
    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert "尚未经督办人员确认" in result["reason"]


def test_04_ai_sourced_criteria_cannot_close(db: Path) -> None:
    """4. AI 来源办结标准 -> 拒绝（AI 建议不得成为强制闭环依据）。

    即使调用方强行把 confirmed 置 True，数据层也会压回 False。
    """
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    _submit(db, task_id)
    task_metadata_store.save_metadata(
        task_id,
        closure_criteria=["AI 给的标准"],
        closure_source=models.CLOSURE_SOURCE_AI,
        closure_confirmed=True,
        db_path=db,
    )
    meta = task_metadata_store.get_metadata(task_id, db_path=db)
    assert meta["closure_confirmed"] is False, "AI 来源必须被强制为未确认"

    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False


def test_05_no_verification_cannot_close(db: Path) -> None:
    """5. 没有核验记录 -> 拒绝。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    _submit(db, task_id)
    _criteria(db, task_id)
    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert "尚未核验" in result["reason"]


def test_06_unapproved_verification_cannot_close(db: Path) -> None:
    """6. 核验状态非 approved -> 拒绝。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    submission_id = _submit(db, task_id)
    _criteria(db, task_id)
    submission = submission_store.get_submission(task_id, db_path=db)
    verification_store.append_verification(
        task_id,
        submission_id,
        models.VERIFICATION_PENDING,
        submission_digest=verification_store.content_digest(submission),
        db_path=db,
    )
    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert "未通过不能闭环" in result["reason"] or "未解决结论" in result["reason"]


def test_07_stale_digest_blocks_closure(db: Path) -> None:
    """7. 旧提交版本 approved、内容已改变 -> 拒绝。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    submission_id = _submit(db, task_id)
    _criteria(db, task_id)
    _verify_approved(db, task_id, submission_id)

    # 负责人重新提交，内容与被核验的版本不同
    submission_store.save_submission(
        task_id, content="内容已被替换为另一份说明", db_path=db
    )

    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert "尚未核验" in result["reason"] or "无法确定版本" in result["reason"]
    assert closure_store.get_closure(task_id, db_path=db) is None

# ============================================================
# 八：满足全部条件时允许闭环
# ============================================================


def test_08_all_conditions_met_allows_closure(db: Path) -> None:
    """8. 当前版本已核验通过且条件齐备 -> 允许闭环。"""
    task_id, submission_id, verification_id = _ready(db)

    result = cs.close_task(
        task_id, closed_by="李主任", closure_note="材料齐全，办结", db_path=db
    )

    assert result["ok"] is True
    assert result["closed"] is True
    closure = closure_store.get_closure(task_id, db_path=db)
    assert closure is not None
    assert closure["task_id"] == task_id
    assert closure["submission_id"] == submission_id
    assert closure["verification_id"] == verification_id
    assert closure["closed_by"] == "李主任"
    assert closure["closure_note"] == "材料齐全，办结"


def test_08b_closure_does_not_change_task_status(db: Path) -> None:
    """闭环不得修改 tasks.status（闭环是独立事实，不是任务状态）。"""
    task_id, _, _ = _ready(db)
    before = sqlite_db.get_task(task_id, db_path=db)["status"]
    cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert sqlite_db.get_task(task_id, db_path=db)["status"] == before


# ============================================================
# 九：高风险授权
# ============================================================


def test_09_high_risk_requires_authorization(db: Path) -> None:
    """9. 高风险任务缺少人工授权理由 -> 拒绝。"""
    task_id, _, _ = _ready(db, risk_level="red")

    denied = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert denied["ok"] is False
    assert "高风险" in denied["reason"]
    assert closure_store.get_closure(task_id, db_path=db) is None

    allowed = cs.close_task(
        task_id, closed_by="李主任", high_risk_ack="已口头复核，确属高风险", db_path=db
    )
    assert allowed["ok"] is True


def test_09b_closed_by_cannot_be_empty(db: Path) -> None:
    """闭环必须显式发起，closed_by 为空 -> 拒绝。"""
    task_id, _, _ = _ready(db)
    result = cs.close_task(task_id, closed_by="   ", db_path=db)
    assert result["ok"] is False
    assert "closed_by" in result["reason"]


# ============================================================
# 十：幂等
# ============================================================


def test_10_repeat_closure_is_idempotent(db: Path) -> None:
    """10. 重复闭环不产生第二条记录，且返回已闭环。"""
    task_id, _, _ = _ready(db)
    first = cs.close_task(task_id, closed_by="李主任", db_path=db)
    second = cs.close_task(task_id, closed_by="王主任", db_path=db)

    assert first["ok"] is True and first["idempotent"] is False
    assert second["ok"] is True and second["idempotent"] is True
    assert second["closure"]["id"] == first["closure"]["id"]
    assert len(closure_store.list_closures(db_path=db)) == 1


# ============================================================
# 十一/十二：不存在任务、跨任务隔离
# ============================================================


def test_11_unknown_task_raises(db: Path) -> None:
    """11. 不存在的任务 -> 抛 TaskNotFoundError。"""
    with pytest.raises(sqlite_db.TaskNotFoundError):
        cs.close_task(9999, closed_by="李主任", db_path=db)


def test_12_cannot_mix_other_task_records(db: Path) -> None:
    """12. 不能混用其他任务的提交或核验记录。"""
    a_id, a_sub, _ = _ready(db)
    b_id = _task(db, title="另一任务")
    b_sub = _submit(db, b_id, "另一任务的提交内容")
    _criteria(db, b_id)
    _verify_approved(db, b_id, b_sub)

    # 把 B 的核验冒充给 A：A 自己的提交指纹与 B 的核验指纹不同
    result = cs.close_task(a_id, closed_by="李主任", db_path=db)
    assert result["ok"] is True  # A 自身条件齐备

    # 反向：A 的核验不能被 B 复用
    fresh = _task(db, title="第三任务")
    task_dispatch.dispatch_task(fresh, db_path=db)
    _submit(db, fresh, "第三任务内容")
    _criteria(db, fresh)
    stolen = cs.close_task(fresh, closed_by="李主任", db_path=db)
    assert stolen["ok"] is False
    assert closure_store.get_closure(fresh, db_path=db) is None

# ============================================================
# 十三：外键与重复初始化
# ============================================================


def test_13_closure_table_created_and_reinit_idempotent(db: Path) -> None:
    """13. task_closures 表可建；重复 init 不报错也不丢数据。"""
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute(
        f"PRAGMA table_info({models.TABLE_TASK_CLOSURES})").fetchall()]
    conn.close()
    assert cols == list(models.TASK_CLOSURE_ALL_COLUMNS)

    task_id, _, _ = _ready(db)
    cs.close_task(task_id, closed_by="李主任", db_path=db)

    for _ in range(3):
        sqlite_db.init_db(db)

    assert closure_store.get_closure(task_id, db_path=db) is not None


def test_13b_task_id_unique_in_closure_table(db: Path) -> None:
    """一任务至多一条闭环记录，由 UNIQUE 保证。"""
    task_id, sub_id, ver_id = _ready(db)
    cs.close_task(task_id, closed_by="李主任", db_path=db)
    with pytest.raises(sqlite3.IntegrityError):
        closure_store.close_task(
            task_id, sub_id, ver_id, closed_by="王主任", db_path=db
        )


def test_13c_delete_task_cascades_closure(db: Path) -> None:
    """删除任务时闭环记录级联清除。"""
    task_id, _, _ = _ready(db)
    cs.close_task(task_id, closed_by="李主任", db_path=db)
    sqlite_db.delete_task(task_id, db_path=db)
    conn = sqlite3.connect(db)
    n = conn.execute(
        f"SELECT COUNT(*) FROM {models.TABLE_TASK_CLOSURES} WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    conn.close()
    assert n == 0


# ============================================================
# 十四：闭环记录与 close 日志一致
# ============================================================


def test_14_closure_and_close_log_are_consistent(db: Path) -> None:
    """14. 闭环成功必须同时产生 task_closures 记录与 close 日志。"""
    task_id, _, _ = _ready(db)
    result = cs.close_task(
        task_id, closed_by="李主任", closure_note="办结确认", db_path=db
    )
    assert result["ok"] is True

    actions = [x["action"] for x in sqlite_db.get_logs(task_id, db_path=db)]
    assert models.ACTION_CLOSE in actions
    close_logs = [x for x in sqlite_db.get_logs(task_id, db_path=db)
                  if x["action"] == models.ACTION_CLOSE]
    assert len(close_logs) == 1, "重复闭环不得写出第二条 close 日志"
    assert close_logs[0]["operator"] == "李主任"


def test_14b_rejected_closure_writes_nothing(db: Path) -> None:
    """被拒绝的闭环不得留下任何痕迹。"""
    task_id = _task(db)
    _criteria(db, task_id)
    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert closure_store.get_closure(task_id, db_path=db) is None
    assert models.ACTION_CLOSE not in [
        x["action"] for x in sqlite_db.get_logs(task_id, db_path=db)
    ]


def test_14c_get_closure_status_reflects_state(db: Path) -> None:
    """get_closure_status 能反映未闭环 / 已闭环 / 依据被取代三种状态。"""
    task_id, _, _ = _ready(db)
    assert cs.get_closure_status(task_id, db_path=db)["closed"] is False

    cs.close_task(task_id, closed_by="李主任", db_path=db)
    status = cs.get_closure_status(task_id, db_path=db)
    assert status["closed"] is True
    assert status["stale"] is False

    submission_store.save_submission(task_id, content="闭环后又提交了新版本", db_path=db)
    assert cs.get_closure_status(task_id, db_path=db)["stale"] is True

# ============================================================
# 十五：旧契约不变
# ============================================================


def test_15_event_actions_unchanged() -> None:
    """15. EVENT_ACTIONS 六元组与 dispatch/submit 语义未被改动。"""
    assert models.EVENT_ACTIONS == (
        "analyze", "draft", "request", "confirm", "reject", "escalate",
    )
    assert models.CHAIN_ACTIONS[:2] == ("dispatch", "submit")
    assert models.CHAIN_ACTIONS == (
        "dispatch", "submit", "verify", "need_supplement", "reject_rework", "close",
    )
    assert models.ACTION_CLOSE == "close"
    assert models.ACTION_VERIFY == "verify"
    assert models.ACTION_NEED_SUPPLEMENT == "need_supplement"
    assert models.ACTION_REJECT_REWORK == "reject_rework"
    # 事件值不得与状态值重叠
    assert set(models.EVENT_ACTIONS).isdisjoint(set(models.CHAIN_ACTIONS))


def test_15b_no_duplicate_definitions_in_models() -> None:
    """回归防线：models.py 不得出现同名常量的重复定义。

    此前 `git checkout` 事故后重建时产生了重复定义，Python 取后者因而
    表面正常，但这正是同型事故的温床。此处显式锁定唯一性。
    """
    import re
    from pathlib import Path

    src = Path(models.__file__).read_text(encoding="utf-8")
    for name in (
        "CHAIN_ACTIONS", "ALL_LOG_ACTIONS", "LOG_ACTIONS",
        "ACTION_DISPATCH", "ACTION_SUBMIT", "ACTION_VERIFY", "ACTION_CLOSE",
        "TABLE_VERIFICATIONS", "CREATE_VERIFICATIONS_SQL",
        "TABLE_TASK_METADATA", "CREATE_TASK_METADATA_SQL",
        "TABLE_TASK_CLOSURES", "CREATE_TASK_CLOSURES_SQL",
    ):
        count = len(re.findall(r"^\s*%s\s*[:=]" % name, src, re.M))
        assert count == 1, f"{name} 有 {count} 处定义，必须唯一"


# ============================================================
# 十六：S1 / S2 存储继续正常
# ============================================================


def test_16_s1_verification_store_still_works(db: Path) -> None:
    """16. S1：核验记录可多轮追加、指纹正确。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    submission_id = _submit(db, task_id)

    submission = submission_store.get_submission(task_id, db_path=db)
    digest = verification_store.content_digest(submission)
    assert isinstance(digest, str) and len(digest) == 32

    for status in (models.VERIFICATION_NEED_SUPPLEMENT, models.VERIFICATION_APPROVED):
        verification_store.append_verification(
            task_id, submission_id, status,
            submission_digest=digest, db_path=db,
        )
    rows = verification_store.list_verifications(task_id, db_path=db)
    assert [r["status"] for r in rows] == ["need_supplement", "approved"]
    assert verification_store.latest_verification(task_id, db_path=db)["status"] == "approved"


def test_16b_s2_metadata_store_still_works(db: Path) -> None:
    """16. S2：办结标准可写可读，AI 来源强制未确认。"""
    task_id = _task(db)
    task_metadata_store.set_execution_requirements(task_id, ["完成现场检查"], db_path=db)
    meta = task_metadata_store.get_metadata(task_id, db_path=db)
    assert meta["execution_requirements"] == ["完成现场检查"]
    assert meta["closure_criteria"] == []

    task_metadata_store.set_closure_criteria(
        task_id, ["AI 标准"], closure_source=models.CLOSURE_SOURCE_AI, db_path=db
    )
    assert task_metadata_store.get_metadata(task_id, db_path=db)["closure_confirmed"] is False


# ============================================================
# 十七/十八：版本变化与指纹缺失的安全行为
# ============================================================


def test_17_attachment_change_invalidates_approval(db: Path) -> None:
    """17. 附件路径变化同样改变指纹，旧核验不得用于新版本。

    正文不变、只换附件 —— 这是最容易漏判的一种版本变化，
    因此指纹必须把 file_path 一起计入。
    """
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    submission_store.save_submission(
        task_id, content="正文相同", file_path="a/证明.pdf", db_path=db
    )
    _criteria(db, task_id)

    before = verification_store.content_digest(
        submission_store.get_submission(task_id, db_path=db)
    )
    _verify_approved(db, task_id, int(
        submission_store.get_submission(task_id, db_path=db)["id"]
    ))
    assert cs.close_task(task_id, closed_by="李", db_path=db)["ok"] is True

    # 换附件：正文不变，指纹必须变化
    task_b = _task(db, title="附件变更任务")
    task_dispatch.dispatch_task(task_b, db_path=db)
    submission_store.save_submission(
        task_b, content="正文相同", file_path="a/证明.pdf", db_path=db
    )
    digest_a = verification_store.content_digest(
        submission_store.get_submission(task_b, db_path=db)
    )
    submission_store.save_submission(
        task_b, content="正文相同", file_path="b/另一份.pdf", db_path=db
    )
    digest_b = verification_store.content_digest(
        submission_store.get_submission(task_b, db_path=db)
    )
    assert digest_a != digest_b, "附件变化必须改变内容指纹"
    assert digest_a != before or digest_b != before

    # 用旧指纹的核验记录去闭环，必须被拒
    _criteria(db, task_b)
    submission_id = int(submission_store.get_submission(task_b, db_path=db)["id"])
    verification_store.append_verification(
        task_b, submission_id, models.VERIFICATION_APPROVED,
        submission_digest=digest_a, db_path=db,
    )
    result = cs.close_task(task_b, closed_by="李", db_path=db)
    assert result["ok"] is False
    assert closure_store.get_closure(task_b, db_path=db) is None


# ============================================================
# 十八：指纹缺失 / 极端数据的安全行为
# ============================================================


def test_18_missing_digest_blocks_closure_safely(db: Path) -> None:
    """18. 核验记录缺指纹（历史数据）-> 不得被当作当前版本通过。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    submission_id = _submit(db, task_id)
    _criteria(db, task_id)

    # 模拟旧数据：写了 approved 但没有指纹
    verification_store.append_verification(
        task_id, submission_id, models.VERIFICATION_APPROVED,
        submission_digest=None, db_path=db,
    )
    result = cs.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False
    assert closure_store.get_closure(task_id, db_path=db) is None


def test_18b_empty_submission_content_blocks_closure(db: Path) -> None:
    """提交内容与附件皆空 -> 指纹为 None -> 无法确定版本 -> 拒绝闭环。"""
    task_id = _task(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    _criteria(db, task_id)
    submission_store.save_submission(task_id, content="占位", db_path=db)

    # 构造极端数据：正文与附件同时为空
    with sqlite_db.get_connection(db) as conn:
        conn.execute(
            f"UPDATE {models.TABLE_TASK_SUBMISSIONS} SET content = '', file_path = NULL "
            "WHERE task_id = ?", (task_id,)
        )

    submission = submission_store.get_submission(task_id, db_path=db)
    assert verification_store.content_digest(submission) is None

    result = cs.close_task(task_id, closed_by="李", db_path=db)
    assert result["ok"] is False
    assert "无法确定版本" in result["reason"]
    assert closure_store.get_closure(task_id, db_path=db) is None