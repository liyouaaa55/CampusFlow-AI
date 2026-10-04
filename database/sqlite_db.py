"""SQLite Agent Memory：任务与操作历史的持久化读写。

设计要点：
1. **不是普通 CRUD**：任务的主数据与状态轨迹（task_logs）成对写入，
   任何状态变更都会自动留痕，Agent（Reminder / Report）后续只看数据库即可复原全貌。
2. **单一数据源**：默认数据库文件取自 config/settings.py 的 DB_PATH；
   所有接口额外提供 db_path 参数，便于测试隔离与多环境部署。
3. **写入即校验**：状态、优先级、风险等级、截止日期格式在写入前校验，
   非法数据直接抛 ValueError，绝不让脏数据进入 Agent 记忆。
4. **事务安全**：批量写入在同一事务内完成，任一条失败则整体回滚。

对外接口：
    init_db()                                  建库建表（幂等）
    create_task(task)                          新增单条任务（自动写 create 日志）
    batch_create(tasks)                        批量新增（单事务，原子）
    get_task(task_id)                          按主键查询
    list_tasks(status, department)              列表查询（支持筛选）
    update_status(task_id, status, operator, remark)  改状态（自动写同状态名日志）
    delete_task(task_id)                       删除任务及其日志
    get_logs(task_id)                          查询某任务的全部操作历史
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from config import settings
from database import models

# 默认数据库文件：<项目根>/data/campusflow.db
DB_PATH: Path = settings.DB_PATH


class DatabaseError(RuntimeError):
    """数据层异常基类。"""


class TaskNotFoundError(DatabaseError):
    """指定的任务不存在（更新状态时必须报错，避免静默失败）。"""


# ============================================================
# 一、连接与时间工具
# ============================================================


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """解析数据库文件路径：未显式指定时使用配置中的 DB_PATH。"""
    return Path(db_path) if db_path else DB_PATH


@contextmanager
def get_connection(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """获取数据库连接的上下文管理器。

    - 自动创建目录与数据表（幂等），避免「no such table」类错误；
    - 开启外键约束（SQLite 默认关闭），保证删除任务时日志级联清理；
    - 正常退出自动提交，异常自动回滚。
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        # 幂等建表：保证任何入口首次调用都能直接使用
        for statement in models.SCHEMA_STATEMENTS:
            connection.execute(statement)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def now_iso() -> str:
    """当前时间字符串（本地时间，秒级精度，格式 2026-09-23 10:30:00）。"""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def init_db(db_path: str | Path | None = None) -> Path:
    """显式初始化数据库：建库、建表、建索引（幂等）。返回数据库文件路径。"""
    with get_connection(db_path):
        pass
    return resolve_db_path(db_path)


# ============================================================
# 二、写入
# ============================================================


def create_task(task: dict[str, Any], db_path: str | Path | None = None) -> dict[str, Any]:
    """新增一条任务，并自动写入一条 create 日志。

    参数：
        task  PlannerAgent 输出或人工修改后的任务字典；
              未提供的字段按默认值处理，未知字段（如 suggestions）会被忽略
    返回：
        入库后的完整记录（含 id、created_at、updated_at）
    异常：
        TypeError    task 不是字典
        ValueError   标题为空，或状态/优先级/风险等级/截止日期/need_confirm 非法
    """
    record = _normalize_task(task)

    with get_connection(db_path) as connection:
        row = _insert_task(connection, record)
        _insert_log(
            connection,
            task_id=row["id"],
            action=models.ACTION_CREATE,
            operator=models.DEFAULT_OPERATOR,
            remark="任务创建",
        )
        created = _fetch_task(connection, row["id"])

    return _row_to_dict(created)


def batch_create(
    tasks: list[dict[str, Any]],
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """批量新增任务（单事务，原子写入）。

    先整体校验再写库：任何一条不合法，全部回滚，不会产生半截数据。
    每条任务同样会自动写入一条 create 日志。
    返回：入库后的记录列表，顺序与入参一致。
    """
    if not isinstance(tasks, list):
        raise TypeError(f"batch_create() 需要列表，收到：{type(tasks).__name__}")
    if not tasks:
        return []

    records = [_normalize_task(task) for task in tasks]

    created_list: list[dict[str, Any]] = []
    with get_connection(db_path) as connection:
        for record in records:
            row = _insert_task(connection, record)
            _insert_log(
                connection,
                task_id=row["id"],
                action=models.ACTION_CREATE,
                operator=models.DEFAULT_OPERATOR,
                remark="任务创建",
            )
            created_list.append(_row_to_dict(_fetch_task(connection, row["id"])))

    return created_list


def update_status(
    task_id: int,
    status: str,
    operator: str = models.DEFAULT_OPERATOR,
    remark: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """更新任务状态，并自动写入一条 action 为新状态的日志。

    例：update_status(1, "completed", operator="李老师", remark="活动已完成")
        自动新增日志：action="completed", operator="李老师", remark="活动已完成"

    返回：更新后的完整记录。
    异常：ValueError 状态非法；TaskNotFoundError 任务不存在。
    """
    _validate_status(status, field="status")

    timestamp = now_iso()
    with get_connection(db_path) as connection:
        existing = _fetch_task(connection, task_id)
        if existing is None:
            raise TaskNotFoundError(f"任务不存在：id={task_id}")

        connection.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, timestamp, int(task_id)),
        )
        _insert_log(
            connection,
            task_id=int(task_id),
            action=status,
            operator=operator,
            remark=remark,
        )
        updated = _fetch_task(connection, task_id)

    return _row_to_dict(updated)


def delete_task(task_id: int, db_path: str | Path | None = None) -> bool:
    """删除任务及其全部日志。

    返回：True 表示删除成功；False 表示任务不存在。
    说明：任务删除后其操作历史一并清除（保持外键一致性），因此删除动作本身不落库。
    """
    with get_connection(db_path) as connection:
        existing = _fetch_task(connection, task_id)
        if existing is None:
            return False

        # 显式清理日志，避免依赖 PRAGMA 设置
        connection.execute("DELETE FROM task_logs WHERE task_id = ?", (int(task_id),))
        connection.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))

    return True


# ============================================================
# 三、查询
# ============================================================


def get_task(task_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """按主键查询任务；不存在返回 None。"""
    with get_connection(db_path) as connection:
        row = _fetch_task(connection, task_id)
    return _row_to_dict(row) if row else None


def list_tasks(
    status: str | None = None,
    department: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """查询任务列表，支持按状态、部门筛选。

    两个条件同时给出时取交集；均为空则返回全部任务（按入库顺序）。
    异常：ValueError 传入未知状态。
    """
    sql = f"SELECT {', '.join(models.TASK_ALL_COLUMNS)} FROM {models.TABLE_TASKS}"
    conditions: list[str] = []
    params: list[Any] = []

    if status is not None:
        _validate_status(status, field="status")
        conditions.append("status = ?")
        params.append(status)

    if department is not None:
        conditions.append("department = ?")
        params.append(department)

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id ASC"

    with get_connection(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_logs(task_id: int, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """查询指定任务的全部操作历史（按时间先后，即 id 升序）。

    任务不存在或尚无日志时返回空列表。
    """
    sql = (
        f"SELECT {', '.join(models.TASK_LOG_COLUMNS)} FROM {models.TABLE_TASK_LOGS} "
        "WHERE task_id = ? ORDER BY id ASC"
    )
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, (int(task_id),)).fetchall()
    return [dict(row) for row in rows]


# ============================================================
# 四、内部实现
# ============================================================


def _insert_task(connection: sqlite3.Connection, record: dict[str, Any]) -> sqlite3.Row:
    """插入一条任务记录，返回含自增 id 的行。"""
    columns = ", ".join(models.TASK_COLUMNS)
    placeholders = ", ".join(["?"] * len(models.TASK_COLUMNS))
    values = [record[column] for column in models.TASK_COLUMNS]

    cursor = connection.execute(
        f"INSERT INTO {models.TABLE_TASKS} ({columns}) VALUES ({placeholders})",
        values,
    )
    row = connection.execute(
        f"SELECT {', '.join(models.TASK_ALL_COLUMNS)} FROM {models.TABLE_TASKS} WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    if row is None:  # 理论上不会发生，留作防御
        raise DatabaseError("任务写入后读取失败。")
    return row


def _insert_log(
    connection: sqlite3.Connection,
    task_id: int,
    action: str,
    operator: str,
    remark: str = "",
) -> None:
    """写入一条操作日志（Agent 记忆轨迹）。"""
    connection.execute(
        f"INSERT INTO {models.TABLE_TASK_LOGS} (task_id, action, operator, remark, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(task_id), action, operator or models.DEFAULT_OPERATOR, remark or "", now_iso()),
    )


def _fetch_task(connection: sqlite3.Connection, task_id: Any) -> sqlite3.Row | None:
    """按 id 读取任务行。"""
    return connection.execute(
        f"SELECT {', '.join(models.TASK_ALL_COLUMNS)} FROM {models.TABLE_TASKS} WHERE id = ?",
        (int(task_id),),
    ).fetchone()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """行转字典，并把 need_confirm 还原为布尔值（与 PlannerAgent 输出一致）。"""
    record = dict(row)
    record["need_confirm"] = bool(record.get("need_confirm"))
    return record


def _normalize_task(task: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化待入库的任务字典。

    未知字段（如 PlannerAgent 的 suggestions）与只读字段（id / created_at / updated_at）
    会被忽略，因此 PlannerAgent 的输出可以直接入库。
    """
    if not isinstance(task, dict):
        raise TypeError(f"任务必须是字典，收到：{type(task).__name__}")

    title = _clean_text(task.get("title"))
    if not title:
        raise ValueError("任务标题不能为空。")

    status = _clean_text(task.get("status")) or models.DEFAULT_STATUS
    _validate_status(status, field="status")

    priority = _clean_text(task.get("priority"))
    if priority is not None and priority not in models.PRIORITIES:
        raise ValueError(f"优先级非法：{priority}（允许值：{'/'.join(models.PRIORITIES)}）")

    risk_level = _clean_text(task.get("risk_level"))
    if risk_level is not None and risk_level not in models.RISK_LEVELS:
        raise ValueError(f"风险等级非法：{risk_level}（允许值：{'/'.join(models.RISK_LEVELS)}）")

    deadline = _normalize_deadline(task.get("deadline"))

    timestamp = now_iso()
    return {
        "title": title,
        "department": _clean_text(task.get("department")),
        "owner": _clean_text(task.get("owner")),
        "deadline": deadline,
        "priority": priority,
        "status": status,
        "risk_level": risk_level,
        "need_confirm": _normalize_need_confirm(task.get("need_confirm")),
        "description": _clean_text(task.get("description")),
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _validate_status(status: Any, field: str = "status") -> None:
    """校验状态取值，非法直接报错。"""
    if status not in models.TASK_STATUSES:
        raise ValueError(
            f"{field} 非法：{status!r}（允许值：{'/'.join(models.TASK_STATUSES)}）"
        )


def _clean_text(value: Any) -> str | None:
    """清洗文本：去首尾空白，空字符串按缺失（None）处理。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_deadline(value: Any) -> str | None:
    """校验截止日期并统一为 YYYY-MM-DD；缺失返回 None。

    只接受具体日期，不接受「尽快」「下周三」这类描述（由上游 Agent 负责换算）。
    """
    text = _clean_text(value)
    if text is None:
        return None

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"截止日期必须为 YYYY-MM-DD 格式：{text!r}") from exc


def _normalize_need_confirm(value: Any) -> int:
    """把 need_confirm 归一化为 0 / 1（兼容布尔、数字与常见文本写法）。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if value in (0, 1):
            return value
        raise ValueError(f"need_confirm 只能为 0 或 1，收到：{value}")

    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "是"):
        return 1
    if text in ("0", "false", "no", "n", "否", ""):
        return 0
    raise ValueError(f"need_confirm 无法解析为布尔值：{value!r}")
