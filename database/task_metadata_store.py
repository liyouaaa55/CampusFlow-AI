"""v2.2 任务办理要求与办结标准的持久化读写（task_metadata）。

与 submission_store / verification_store 的关系（三者并列，边界一致）：
    本模块只负责 task_metadata 一张表，不触碰 tasks / task_logs /
    task_submissions / verifications，也不给任何既有模块加分支。

数据层只做四件事：保存、读取、更新、结构校验。
**不做业务判断**——「办结标准该是什么」属于 S3 与督办人员的职责。

两条由代码保证的硬约束（不靠调用方自觉）：

    1. **一个任务至多一条元数据**：task_id 带 UNIQUE，写入走
       UPDATE ... WHERE task_id = ?，重复保存不会产生第二行。

    2. **AI 建议永远处于未确认状态**：closure_source="ai" 时，
       closure_confirmed 强制为 False。这条写在 _enforce_source_confirmed() 里，
       即使调用方显式传 confirmed=True 也会被压回 False——
       「未经督办人员确认的 AI 建议不得作为强制办结标准」是业务红线，
       必须是结构性的，而不是文档约定。

兼容性：任务没有元数据行时，读取返回**安全默认值**（空列表 + 未确认），
绝不抛异常、绝不返回 None，保证旧任务与既有页面链路完全不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from database import models
from database.sqlite_db import TaskNotFoundError, get_connection, now_iso

MAX_TEXT_CHARS: int = 2000
MAX_ITEMS: int = 50


class TaskMetadataError(RuntimeError):
    """任务元数据层异常基类。"""


def _require_task(connection: Any, task_id: int) -> None:
    """确认任务存在，否则抛 TaskNotFoundError（严禁孤儿子记录）。"""
    row = connection.execute(
        f"SELECT id FROM {models.TABLE_TASKS} WHERE id = ?", (int(task_id),)
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"任务不存在：id={task_id}")

def _default_metadata(task_id: int) -> dict[str, Any]:
    """旧任务（无元数据行）读取时的安全默认值。

    绝不返回 None：调用方（页面/未来的 Verification Service）无需判空。
    """
    return {
        "id": None,
        "task_id": int(task_id),
        "execution_requirements": [],
        "closure_criteria": [],
        "closure_source": None,
        "closure_confirmed": False,
        "created_at": None,
        "updated_at": None,
    }


def _clean_list(value: Any, field: str) -> list[str]:
    """把办理要求/办结标准归一为字符串列表。

    容错：入参可为字符串（按行拆分）、可为空、None。
    超长截断，元素超长截断；非字符串元素直接字符串化，不猜测其含义。
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = [line for line in value.replace("；", "\n").split("\n")]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raise TaskMetadataError(f"{field} 必须是字符串或字符串列表，收到：{type(value).__name__}")

    items: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            items.append(text[:MAX_TEXT_CHARS])
        if len(items) >= MAX_ITEMS:
            break
    return items


def _dump_list(items: list[str]) -> str | None:
    """列表 → JSON 文本；空列表存 NULL 而不是 "[]"，保持表内容干净。"""
    return json.dumps(items, ensure_ascii=False) if items else None


def _load_list(raw: Any) -> list[str]:
    """JSON 文本 → 列表。

    **损坏数据不得抛出**：存量库里若有人手工改坏过 JSON，
    这里降级为返回空列表，保证整个任务查询链路不崩。
    """
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x or "").strip()]

def _enforce_source_confirmed(source: str | None, confirmed: bool) -> tuple[str | None, bool]:
    """来源与确认状态的强制约束。

    业务红线：**未经督办人员确认的 AI 建议不得作为强制办结标准。**
    因此 closure_source="ai" 时，closure_confirmed 一律压回 False，
    即使调用方显式传 True。督办人员确认后应把来源改为 manual。
    """
    if source is None:
        return None, False
    text = str(source).strip()
    if text not in models.CLOSURE_SOURCES:
        raise TaskMetadataError(
            f"办结标准来源非法：{source!r}（允许值：{'/'.join(models.CLOSURE_SOURCES)}）"
        )
    if text == models.CLOSURE_SOURCE_AI:
        return text, False
    return text, bool(confirmed)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "execution_requirements": _load_list(row["execution_requirements"]),
        "closure_criteria": _load_list(row["closure_criteria"]),
        "closure_source": row["closure_source"],
        "closure_confirmed": bool(row["closure_confirmed"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


_SELECT = (
    f"SELECT {', '.join(models.TASK_METADATA_ALL_COLUMNS)} "
    f"FROM {models.TABLE_TASK_METADATA}"
)


# ============================================================
# 一、读取
# ============================================================


def get_metadata(
    task_id: int,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """读取任务元数据；没有记录时返回安全默认值（旧任务兼容的关键）。"""
    with get_connection(db_path) as connection:
        row = connection.execute(
            f"{_SELECT} WHERE task_id = ?", (int(task_id),)
        ).fetchone()
    return _row_to_dict(row) if row is not None else _default_metadata(task_id)


def has_metadata(task_id: int, db_path: str | Path | None = None) -> bool:
    """该任务是否已有元数据行（用于区分「没有」与「有但为空」）。"""
    return get_metadata(task_id, db_path=db_path)["id"] is not None

# ============================================================
# 二、保存 / 更新（同一任务始终只有一行）
# ============================================================


def save_metadata(
    task_id: int,
    *,
    execution_requirements: Any = None,
    closure_criteria: Any = None,
    closure_source: str | None = None,
    closure_confirmed: bool = False,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """保存或更新任务元数据，返回落库后的记录。

    并发安全：先 UPDATE，未命中再 INSERT；UNIQUE 约束兜底，
    因此重复保存不会产生第二行。

    异常：
        TaskNotFoundError  任务不存在（与既有 store 约定一致）
        TaskMetadataError  来源非法 / 字段结构非法
    """
    requirements = _clean_list(execution_requirements, "execution_requirements")
    criteria = _clean_list(closure_criteria, "closure_criteria")
    source, confirmed = _enforce_source_confirmed(closure_source, closure_confirmed)
    stamp = now_iso()

    with get_connection(db_path) as connection:
        _require_task(connection, task_id)
        cursor = connection.execute(
            f"UPDATE {models.TABLE_TASK_METADATA} SET "
            "execution_requirements = ?, closure_criteria = ?, closure_source = ?, "
            "closure_confirmed = ?, updated_at = ? WHERE task_id = ?",
            (
                _dump_list(requirements),
                _dump_list(criteria),
                source,
                1 if confirmed else 0,
                stamp,
                int(task_id),
            ),
        )
        if cursor.rowcount == 0:
            connection.execute(
                f"INSERT INTO {models.TABLE_TASK_METADATA} ("
                "task_id, execution_requirements, closure_criteria, "
                "closure_source, closure_confirmed, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(task_id),
                    _dump_list(requirements),
                    _dump_list(criteria),
                    source,
                    1 if confirmed else 0,
                    stamp,
                    stamp,
                ),
            )
        row = connection.execute(
            f"{_SELECT} WHERE task_id = ?", (int(task_id),)
        ).fetchone()

    return _row_to_dict(row)


def set_execution_requirements(
    task_id: int, requirements: Any, db_path: str | Path | None = None
) -> dict[str, Any]:
    """只更新办理要求，保留既有办结标准与来源。"""
    current = get_metadata(task_id, db_path=db_path)
    return save_metadata(
        task_id,
        execution_requirements=requirements,
        closure_criteria=current["closure_criteria"],
        closure_source=current["closure_source"],
        closure_confirmed=current["closure_confirmed"],
        db_path=db_path,
    )


def set_closure_criteria(
    task_id: int,
    criteria: Any,
    *,
    closure_source: str,
    closure_confirmed: bool = False,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """写入办结标准，并按来源强制确认状态（AI 来源强制未确认）。"""
    current = get_metadata(task_id, db_path=db_path)
    return save_metadata(
        task_id,
        execution_requirements=current["execution_requirements"],
        closure_criteria=criteria,
        closure_source=closure_source,
        closure_confirmed=closure_confirmed,
        db_path=db_path,
    )


def delete_metadata(task_id: int, db_path: str | Path | None = None) -> bool:
    """删除该任务的元数据行，返回是否真的删掉了。"""
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            f"DELETE FROM {models.TABLE_TASK_METADATA} WHERE task_id = ?",
            (int(task_id),),
        )
    return int(cursor.rowcount) > 0