"""v3.0 第二阶段：执行反馈的持久化读写（task_feedback，追加式）。

与既有 store 的边界（四者并列，互不替代）：
    task_submissions  我交了什么 —— 核验 / 正式闭环的锚点，一任务一行
    task_feedback     执行进展如何 —— 追加式，最新一条为「当前有效反馈」
    verifications     交的东西算不算办结
    task_closures     是否已正式办结

为什么是追加式而不是覆盖式：
    管理者在任务执行期间可能多次录入反馈（进行中 → 进行中 → 已完成）。
    覆盖式会丢掉轨迹，也无法回答「任务是什么时候变成已完成的」。
    追加式保留完整轨迹，并由 latest_feedback() 明确给出当前有效反馈。

本模块只做持久化：不做任何业务判断（「这次反馈该不该改任务状态」属于 service 层）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from database import models
from database.sqlite_db import TaskNotFoundError, get_connection, now_iso

MAX_NOTE_CHARS: int = 2000
MAX_OPERATOR_CHARS: int = 64


class FeedbackError(RuntimeError):
    """执行反馈数据层异常基类。"""


def _require_task(connection: Any, task_id: int) -> None:
    """确认任务存在，否则抛 TaskNotFoundError（严禁孤儿子记录）。"""
    row = connection.execute(
        f"SELECT id FROM {models.TABLE_TASKS} WHERE id = ?", (int(task_id),)
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"任务不存在：id={task_id}")


def _clean(value: Any, limit: int) -> str | None:
    """清洗可选文本：去首尾空白、超长截断，空值返回 None。"""
    text = str(value or "").strip()
    return text[:limit] if text else None


def _clean_attachment(value: Any) -> str | None:
    """附件只保留文件名，杜绝路径穿越（与 submission_store 同口径）。"""
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text).name


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "status": str(row["status"]),
        "note": row["note"],
        "file_path": row["file_path"],
        "operator": row["operator"],
        "created_at": str(row["created_at"]),
    }


_SELECT = (
    f"SELECT {', '.join(models.TASK_FEEDBACK_ALL_COLUMNS)} "
    f"FROM {models.TABLE_TASK_FEEDBACK}"
)


# ============================================================
# 一、写入（追加，绝不覆盖）
# ============================================================


def append_feedback(
    task_id: int,
    status: str,
    *,
    note: str = "",
    file_path: str | Path | None = None,
    operator: str = "",
    created_at: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """追加一条执行反馈并返回该记录。

    参数：
        task_id    任务；不存在抛 TaskNotFoundError
        status     必须是 models.FEEDBACK_STATUSES 之一
        note       工作说明（选填）
        file_path  附件（选填）；只保留文件名
        operator   反馈录入人
        created_at 反馈时间；默认当前时间（测试可注入固定值）

    异常：TaskNotFoundError 任务不存在；FeedbackError 状态非法。
    """
    if status not in models.FEEDBACK_STATUSES:
        raise FeedbackError(
            f"反馈状态非法：{status!r}（允许值：{'/'.join(models.FEEDBACK_STATUSES)}）"
        )

    stamp = created_at or now_iso()
    with get_connection(db_path) as connection:
        _require_task(connection, task_id)
        cursor = connection.execute(
            f"INSERT INTO {models.TABLE_TASK_FEEDBACK} "
            "(task_id, status, note, file_path, operator, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(task_id),
                str(status),
                _clean(note, MAX_NOTE_CHARS),
                _clean_attachment(file_path),
                _clean(operator, MAX_OPERATOR_CHARS),
                stamp,
            ),
        )
        row = connection.execute(
            f"{_SELECT} WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()

    return _row_to_dict(row)


# ============================================================
# 二、读取
# ============================================================


def list_feedback(task_id: int, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """按时间正序返回某任务的全部反馈（轨迹：进行中 → … → 已完成）。"""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"{_SELECT} WHERE task_id = ? ORDER BY id ASC", (int(task_id),)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def latest_feedback(task_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """取某任务的**当前有效反馈**（最新一条）；从未反馈返回 None。"""
    with get_connection(db_path) as connection:
        row = connection.execute(
            f"{_SELECT} WHERE task_id = ? ORDER BY id DESC LIMIT 1", (int(task_id),)
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def latest_feedback_map(db_path: str | Path | None = None) -> dict[int, dict[str, Any]]:
    """一次性取全部任务的当前有效反馈：{task_id: 记录}。

    供统计与报告使用（避免逐任务查询造成 N+1）。
    """
    with get_connection(db_path) as connection:
        rows = connection.execute(f"{_SELECT} ORDER BY id ASC").fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        record = _row_to_dict(row)
        result[record["task_id"]] = record  # id 升序，后者覆盖前者 = 最新
    return result


def count_feedback(task_id: int | None = None, db_path: str | Path | None = None) -> int:
    """统计反馈条数；task_id 为 None 时统计全部。"""
    with get_connection(db_path) as connection:
        if task_id is None:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM {models.TABLE_TASK_FEEDBACK}"
            ).fetchone()
        else:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM {models.TABLE_TASK_FEEDBACK} WHERE task_id = ?",
                (int(task_id),),
            ).fetchone()
    return int(row["n"])


def delete_feedback(task_id: int, db_path: str | Path | None = None) -> int:
    """删除某任务的全部反馈，返回删除条数（演示数据重置与测试清理用）。"""
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            f"DELETE FROM {models.TABLE_TASK_FEEDBACK} WHERE task_id = ?", (int(task_id),)
        )
    return int(cursor.rowcount)
