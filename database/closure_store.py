"""v2.2 正式闭环记录的持久化读写（task_closures）。

与前三个 store 并列，边界一致：只负责一张表，不改任何既有表与既有模块。

为什么单独建表而不是扩展 tasks.status：
    1. tasks.status 六个取值无闭环态，新增需 ALTER TABLE 迁移；
    2. tasks 字段顺序被 5 处既有测试逐值锁定；
    3. 闭环是独立于「执行状态」的业务事实，塞进 status 会污染
       reminder_agent 排除集、report_agent 完成率与 submission_check 判定。

一任务至多一次闭环由 task_id UNIQUE 保证；重复闭环在 SQL 层即被挡住。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from database import models
from database.sqlite_db import TaskNotFoundError, get_connection, now_iso

MAX_NOTE_CHARS: int = 1000
MAX_CLOSED_BY_CHARS: int = 64


class ClosureError(RuntimeError):
    """闭环数据层异常基类。"""


def _require_task(connection: Any, task_id: int) -> None:
    row = connection.execute(
        f"SELECT id FROM {models.TABLE_TASKS} WHERE id = ?", (int(task_id),)
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"任务不存在：id={task_id}")


def _clean(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


_SELECT = (
    f"SELECT {', '.join(models.TASK_CLOSURE_ALL_COLUMNS)} "
    f"FROM {models.TABLE_TASK_CLOSURES}"
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "submission_id": int(row["submission_id"]) if row["submission_id"] is not None else None,
        "submission_digest": row["submission_digest"],
        "verification_id": int(row["verification_id"]),
        "closed_by": row["closed_by"],
        "closed_at": str(row["closed_at"]),
        "closure_note": row["closure_note"],
        "created_at": str(row["created_at"]),
    }

# ============================================================
# 一、读取
# ============================================================


def get_closure(task_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """取该任务的正式闭环记录；未闭环返回 None。"""
    with get_connection(db_path) as connection:
        row = connection.execute(
            f"{_SELECT} WHERE task_id = ?", (int(task_id),)).fetchone()
    return _row_to_dict(row) if row is not None else None


def is_closed(task_id: int, db_path: str | Path | None = None) -> bool:
    return get_closure(task_id, db_path=db_path) is not None


def list_closures(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(f"{_SELECT} ORDER BY id ASC").fetchall()
    return [_row_to_dict(r) for r in rows]


# ============================================================
# 二、写入（正式闭环）
# ============================================================


def close_task(
    task_id: int,
    submission_id: int,
    verification_id: int,
    *,
    submission_digest: str | None = None,
    closed_by: str,
    closure_note: str = "",
    closed_at: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """写入一条正式闭环记录。

    前置校验全部由 tools/closure_service 完成，这里只负责落库。
    task_id UNIQUE：重复闭环会触发 IntegrityError，由调用方转成幂等结果。
    """
    stamp = now_iso()
    moment = closed_at or stamp
    with get_connection(db_path) as connection:
        _require_task(connection, task_id)
        connection.execute(
            f"INSERT INTO {models.TABLE_TASK_CLOSURES} ("
            "task_id, submission_id, submission_digest, verification_id, "
            "closed_by, closed_at, closure_note, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(task_id),
                int(submission_id),
                submission_digest,
                int(verification_id),
                _clean(closed_by, MAX_CLOSED_BY_CHARS),
                moment,
                _clean(closure_note, MAX_NOTE_CHARS),
                stamp,
            ),
        )
        row = connection.execute(
            f"{_SELECT} WHERE task_id = ?", (int(task_id),)).fetchone()
    return _row_to_dict(row)