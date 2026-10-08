"""v2.1 工作回执的持久化读写（task_submissions）。

与 sqlite_db.py 的关系（刻意与 2.0 保持边界）：
    本模块是**并行的第二入口**，只负责 task_submissions 一张表。
    既有 sqlite_db.py 无需为此改动任何原有函数——它只在「任务下发 / 回执提交」
    时借道 append_chain_log() 往 task_logs 补一条执行链路日志，
    那既是 v2.0 已有的日志表，也不属于本模块的职责。

与 sqlite_db.py 保持一致的三条约定：
    1. 一切读写都走 sqlite_db.get_connection()，因此同样享有外键约束、
       WAL、写锁等待与「异常自动回滚」的事务保证；
    2. 时间戳统一复用 sqlite_db.now_iso()，与 task_logs 完全同格式；
    3. 任务不存在时一律抛 TaskNotFoundError，绝不留下孤儿子记录。

唯一性由数据库保证：task_id 列带 UNIQUE 约束（见 models 的建表 DDL），
「一项任务一条回执」不依赖应用层自觉，重复提交在 SQL 层就被挡住。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from database import models
from database.sqlite_db import TaskNotFoundError, get_connection, now_iso

MAX_CONTENT_CHARS: int = 5000


class SubmissionError(RuntimeError):
    """回执数据层异常基类。"""


class ContentTooLongError(SubmissionError):
    """回执内容超出长度上限。"""


def _require_task(connection: Any, task_id: int) -> None:
    """确认任务存在，否则抛 TaskNotFoundError（严禁孤儿子记录）。"""
    row = connection.execute(
        f"SELECT id FROM {models.TABLE_TASKS} WHERE id = ?", (int(task_id),)
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"任务不存在：id={task_id}")


def _normalize_content(value: Any) -> str:
    """清洗回执正文：去首尾空白、校验非空与长度。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("完成情况不能为空。")
    if len(text) > MAX_CONTENT_CHARS:
        raise ContentTooLongError(
            f"完成情况不得超过 {MAX_CONTENT_CHARS} 字，当前 {len(text)} 字。"
        )
    return text


def _normalize_file_path(value: Any) -> str | None:
    """清洗附件路径：缺失返回 None；有值则只保留文件名，杜绝路径穿越。"""
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text).name


# ============================================================
# 二、写入
# ============================================================


def save_submission(
    task_id: int,
    content: str,
    file_path: str | Path | None = None,
    submitted_at: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """提交（或更新）一条工作回执。

    **upsert 语义**：一个任务只有一条回执。
    - 首次提交 → INSERT；
    - 重复提交 → UPDATE 同一行，并把 submitted_at 刷新为本次时间。

    这样既满足「不产生重复 Submission」，又允许负责人补填内容或补传附件，
    而不是在第二次提交时把人挡在门外。

    参数：
        task_id      任务；不存在抛 TaskNotFoundError
        content      完成情况；空抛 ValueError，超长抛 ContentTooLongError
        file_path    附件路径（可选）；只取文件名，杜绝路径穿越
        submitted_at 提交时间；默认取当前时间（测试可注入固定值）

    返回：入库后的完整回执记录。
    """
    text = _normalize_content(content)
    attachment = _normalize_file_path(file_path)
    moment = submitted_at or now_iso()
    timestamp = now_iso()

    with get_connection(db_path) as connection:
        _require_task(connection, task_id)

        existing = connection.execute(
            f"SELECT id FROM {models.TABLE_TASK_SUBMISSIONS} WHERE task_id = ?",
            (int(task_id),),
        ).fetchone()

        if existing is None:
            cursor = connection.execute(
                f"INSERT INTO {models.TABLE_TASK_SUBMISSIONS} "
                "(task_id, content, file_path, status, submitted_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(task_id),
                    text,
                    attachment,
                    models.SUBMISSION_SUBMITTED,
                    moment,
                    timestamp,
                    timestamp,
                ),
            )
            row_id = cursor.lastrowid
        else:
            # 附件为空时保留原有附件，避免「补填文字把附件弄丢」
            kept = attachment
            if kept is None:
                previous = connection.execute(
                    f"SELECT file_path FROM {models.TABLE_TASK_SUBMISSIONS} WHERE id = ?",
                    (existing["id"],),
                ).fetchone()
                kept = previous["file_path"] if previous else None
            connection.execute(
                f"UPDATE {models.TABLE_TASK_SUBMISSIONS} SET content = ?, file_path = ?, "
                "status = ?, submitted_at = ?, updated_at = ? WHERE id = ?",
                (text, kept, models.SUBMISSION_SUBMITTED, moment, timestamp, existing["id"]),
            )
            row_id = existing["id"]

        row = connection.execute(
            f"SELECT {', '.join(models.SUBMISSION_ALL_COLUMNS)} "
            f"FROM {models.TABLE_TASK_SUBMISSIONS} WHERE id = ?",
            (row_id,),
        ).fetchone()

    return dict(row)


def delete_submission(task_id: int, db_path: str | Path | None = None) -> bool:
    """删除某任务的回执，返回是否真的删掉了。

    仅用于演示前重置数据与测试清理；正常业务流程不需要它。
    """
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            f"DELETE FROM {models.TABLE_TASK_SUBMISSIONS} WHERE task_id = ?",
            (int(task_id),),
        )
        return cursor.rowcount > 0


# ============================================================
# 三、查询
# ============================================================


def get_submission(
    task_id: int,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """查询某任务的回执；未提交返回 None。"""
    sql = (
        f"SELECT {', '.join(models.SUBMISSION_ALL_COLUMNS)} "
        f"FROM {models.TABLE_TASK_SUBMISSIONS} WHERE task_id = ? LIMIT 1"
    )
    with get_connection(db_path) as connection:
        row = connection.execute(sql, (int(task_id),)).fetchone()
    return dict(row) if row else None


def list_submissions(
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """列出全部回执（按任务 id 升序），供执行检查汇总使用。"""
    sql = (
        f"SELECT {', '.join(models.SUBMISSION_ALL_COLUMNS)} "
        f"FROM {models.TABLE_TASK_SUBMISSIONS} ORDER BY task_id ASC"
    )
    with get_connection(db_path) as connection:
        rows = connection.execute(sql).fetchall()
    return [dict(row) for row in rows]


def count_submissions(db_path: str | Path | None = None) -> int:
    """统计已提交回执总数，供执行检查汇总使用。"""
    with get_connection(db_path) as connection:
        row = connection.execute(
            f"SELECT COUNT(*) AS n FROM {models.TABLE_TASK_SUBMISSIONS}"
        ).fetchone()
    return int(row["n"]) if row else 0
