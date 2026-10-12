"""v2.2 结果核验的持久化读写（verifications）。

与 submission_store.py 的关系（刻意与 2.0 / 2.1 保持边界）：
    本模块是**并行的第三入口**，只负责 verifications 一张表。
    既有 sqlite_db.py / submission_store.py 无需为此改动任何原有函数。

与既有 store 完全一致的约定：
    1. 一切读写都走 sqlite_db.get_connection()，享有同样的外键约束、
       WAL、写锁等待与「异常自动回滚」的事务保证；
    2. 时间戳统一复用 sqlite_db.now_iso()，与 task_logs 完全同格式；
    3. 任务不存在时一律抛 TaskNotFoundError，绝不留下孤儿子记录；
    4. 枚举值一律白名单校验，非法值直接拒绝。

**本模块只做持久化，不做任何业务判断。**
「这次核验该给通过还是驳回」属于 S3 的 Verification Service 职责；
「已提交 不等于 已闭环」的语义由 verifications.status 独立承载，
不塞进 tasks.status，也不改写 task_submissions.status。
"""

from __future__ import annotations

import hashlib
from typing import Any

from database import models
from database.sqlite_db import TaskNotFoundError, get_connection, now_iso

MAX_REASON_CHARS: int = 2000


def content_digest(submission: Any) -> str | None:
    """提交内容的稳定指纹（sha256 前 32 位）。

    只由内容本身决定：同一秒内两次提交时间戳可能相同，
    且时间戳可被测试注入，因此不能用时间戳标识版本。
    内容相同 -> 指纹相同（重复提交同样内容不算新版本）。
    """
    if not submission:
        return None
    body = str(submission.get("content") or "").strip()
    attach = str(submission.get("file_path") or "").strip()
    if not body and not attach:
        return None
    raw = f"{body}\x1f{attach}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _ensure_digest_column(connection: Any) -> None:
    """为已存在的 verifications 表补齐 submission_digest 列（幂等、只增不改）。

    CREATE TABLE IF NOT EXISTS 对既有表是空操作，因此历史库需要这一步。
    只做 ADD COLUMN，不放宽任何既有约束。
    """
    cols = {r[1] for r in connection.execute(
        f"PRAGMA table_info({models.TABLE_VERIFICATIONS})").fetchall()}
    if models.VERIFICATION_SUBMISSION_DIGEST in cols:
        return
    connection.execute(
        f"ALTER TABLE {models.TABLE_VERIFICATIONS} "
        f"ADD COLUMN {models.VERIFICATION_SUBMISSION_DIGEST} TEXT"
    )
MAX_VERIFIED_BY_CHARS: int = 64


class VerificationError(RuntimeError):
    """核验数据层异常基类。"""


def _require_task(connection: Any, task_id: int) -> None:
    """确认任务存在，否则抛 TaskNotFoundError（严禁孤儿子记录）。"""
    row = connection.execute(
        f"SELECT id FROM {models.TABLE_TASKS} WHERE id = ?", (int(task_id),)
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"任务不存在：id={task_id}")


def _require_submission(connection: Any, submission_id: int, task_id: int) -> None:
    """确认回执存在且属于该任务，否则抛 VerificationError。

    核验必须锚定到一次具体提交，否则「核验的是哪一版结果」无从追溯。
    """
    row = connection.execute(
        f"SELECT id, task_id FROM {models.TABLE_TASK_SUBMISSIONS} WHERE id = ?",
        (int(submission_id),),
    ).fetchone()
    if row is None:
        raise VerificationError(f"回执不存在：id={submission_id}")
    if int(row["task_id"]) != int(task_id):
        raise VerificationError(f"回执 {submission_id} 不属于任务 {task_id}，拒绝核验。")


def _clean(value: Any, limit: int) -> str | None:
    """清洗可选文本：去首尾空白，超长截断，空值返回 None。"""
    text = str(value or "").strip()
    return text[:limit] if text else None


def _flag(value: Any) -> int:
    """核验四维标记统一归一为 0 / 1，绝不存 None 或其他真值。"""
    return 1 if value else 0


def _row_to_dict(row: Any) -> dict[str, Any]:
    """行 → 字典，并把四个维度还原为布尔语义供业务层直接使用。"""
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "submission_id": int(row["submission_id"]) if row["submission_id"] is not None else None,
        "status": str(row["status"]),
        "submission_digest": _row_value(row, models.VERIFICATION_SUBMISSION_DIGEST),
        "reason": row["reason"],
        "requirement_match": bool(row["requirement_match"]),
        "deliverable_complete": bool(row["deliverable_complete"]),
        "process_valid": bool(row["process_valid"]),
        "state_consistent": bool(row["state_consistent"]),
        "verified_by": row["verified_by"],
        "verified_at": str(row["verified_at"]),
    }


# ============================================================
# 一、写入
# ============================================================


def append_verification(
    task_id: int,
    submission_id: int,
    status: str,
    *,
    reason: str = "",
    requirement_match: bool = False,
    deliverable_complete: bool = False,
    process_valid: bool = False,
    state_consistent: bool = False,
    verified_by: str = "",
    submission_digest: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """追加一条核验记录并返回。

    刻意**不**做 upsert：同一任务允许有多条核验，完整轨迹为
        submission#1 → verification#1(需补充) → verification#2(通过)
    「驳回 → 补充 → 再核验」是本系统的核心闭环路径，
    若按 task_id 覆盖，历史核验轨迹将无法留痕。

    参数：
        task_id / submission_id  必须存在且互相归属
        status                    必须是 models.VERIFICATION_STATUSES 之一
        reason                    核验理由（会被截断到 MAX_REASON_CHARS）
        requirement_match         办理要求是否满足
        deliverable_complete      办结材料是否齐全
        process_valid            办理过程是否合规
        state_consistent          状态是否一致
        verified_by               核验人（人工复核时填写）

    异常：
        TaskNotFoundError   任务不存在
        VerificationError   回执不存在 / 归属不符 / 状态非法
    """
    if status not in models.VERIFICATION_STATUSES:
        raise VerificationError(
            f"核验状态非法：{status!r}（允许值：{'/'.join(models.VERIFICATION_STATUSES)}）"
        )

    stamp = now_iso()
    with get_connection(db_path) as connection:
        _ensure_digest_column(connection)
        _require_task(connection, task_id)
        _require_submission(connection, submission_id, task_id)
        cursor = connection.execute(
            f"INSERT INTO {models.TABLE_VERIFICATIONS} ("
            f"task_id, submission_id, submission_digest, status, reason, requirement_match, "
            f"deliverable_complete, process_valid, state_consistent, "
            f"verified_by, verified_at"
            f") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(task_id),
                int(submission_id),
                submission_digest,
                str(status),
                _clean(reason, MAX_REASON_CHARS),
                _flag(requirement_match),
                _flag(deliverable_complete),
                _flag(process_valid),
                _flag(state_consistent),
                _clean(verified_by, MAX_VERIFIED_BY_CHARS),
                stamp,
            ),
        )
        row = connection.execute(
            f"SELECT {', '.join(models.VERIFICATION_ALL_COLUMNS)} "
            f"FROM {models.TABLE_VERIFICATIONS} WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()

    return _row_to_dict(row)


# ============================================================
# 二、读取
# ============================================================

def _row_value(row: Any, key: str) -> Any:
    """按列名安全取值：历史库若尚未补齐该列则返回 None。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


_SELECT = (
    f"SELECT {', '.join(models.VERIFICATION_ALL_COLUMNS)} "
    f"FROM {models.TABLE_VERIFICATIONS}"
)


def get_verification(
    verification_id: int,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """按主键取单条核验记录；不存在返回 None。"""
    with get_connection(db_path) as connection:
        _ensure_digest_column(connection)
        row = connection.execute(
            f"{_SELECT} WHERE id = ?", (int(verification_id),)
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_verifications(
    task_id: int,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """按时间正序返回某任务的全部核验记录。

    正序而非倒序：调用方要展示的是「第一次核验 → 需补充 → 第二次核验 → 通过」
    这条轨迹的先后关系，倒序会把它读反。
    """
    with get_connection(db_path) as connection:
        _ensure_digest_column(connection)
        rows = connection.execute(
            f"{_SELECT} WHERE task_id = ? ORDER BY id ASC", (int(task_id),)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def latest_verification(
    task_id: int,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """取该任务最新一条核验记录；从未核验则返回 None。"""
    with get_connection(db_path) as connection:
        _ensure_digest_column(connection)
        row = connection.execute(
            f"{_SELECT} WHERE task_id = ? ORDER BY id DESC LIMIT 1", (int(task_id),)
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def count_verifications(
    task_id: int | None = None,
    db_path: str | Path | None = None,
) -> int:
    """统计核验记录数；task_id 为 None 时统计全部。"""
    with get_connection(db_path) as connection:
        _ensure_digest_column(connection)
        if task_id is None:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM {models.TABLE_VERIFICATIONS}"
            ).fetchone()
        else:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM {models.TABLE_VERIFICATIONS} WHERE task_id = ?",
                (int(task_id),),
            ).fetchone()
    return int(row["n"])


def delete_verifications(
    task_id: int,
    db_path: str | Path | None = None,
) -> int:
    """删除某任务的全部核验记录，返回删除条数。

    与 task 级联删除并存：手工清理演示数据时使用。
    """
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            f"DELETE FROM {models.TABLE_VERIFICATIONS} WHERE task_id = ?",
            (int(task_id),),
        )
    return int(cursor.rowcount)
