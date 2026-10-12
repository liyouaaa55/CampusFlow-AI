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
    update_risk_level(task_id, risk_level, operator, remark)  改风险等级（自动写 escalate 日志）
    append_log(task_id, action, operator, remark)  追加一条日志（不改 tasks 表）
    append_chain_log(task_id, action, ...)          追加执行链路日志（v2.1：dispatch/submit）
    delete_task(task_id)                       删除任务及其日志
    get_logs(task_id)                          查询某任务的全部操作历史

并发约定（v2.0）：
    - CONNECTION_TIMEOUT：写锁等待时间，避免并发写直接报 database is locked；
    - WAL 日志模式：读写不互斥；
    - update_status / update_risk_level 均带乐观锁，冲突时抛
      ConcurrentUpdateError，绝不静默覆盖他人修改。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from config import settings
from database import models

# 默认数据库文件：<项目根>/data/campusflow.db
DB_PATH: Path = settings.DB_PATH

# 写锁等待时间（秒）。
# 作用：多个连接同时写入时，SQLite 默认会立即返回 "database is locked"。
# 设为 10 秒后，写方先等待锁释放，显著降低「并发写」场景下的失败率。
# 该参数等价于 PRAGMA busy_timeout，作用于每一次新建连接。
CONNECTION_TIMEOUT: float = 10.0

# 是否启用 WAL（Write-Ahead Logging，预写日志）模式。
# 收益：读写不互斥，多读者与单写者可并发，写入吞吐优于默认回滚日志。
# 代价：数据库文件旁会额外出现 <db>-wal 与 <db>-shm 两个文件（由 SQLite 自动管理）。
WAL_MODE_ENABLED: bool = True


class DatabaseError(RuntimeError):
    """数据层异常基类。"""


class TaskNotFoundError(DatabaseError):
    """指定的任务不存在（更新状态时必须报错，避免静默失败）。"""


class ConcurrentUpdateError(DatabaseError):
    """并发冲突：目标记录在本次读取之后已被其他操作修改，写入被拒绝。

    用于乐观锁失败时的显式报错——宁可让调用方知道「有人抢先改过」，
    也绝不静默覆盖他人的修改结果。
    """


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
    - 设置 CONNECTION_TIMEOUT 写锁等待时间，避免并发写直接失败；
    - 启用 WAL 日志模式，降低读写互相阻塞的概率；
    - 正常退出自动提交，异常自动回滚（保证「写数据 + 写日志」原子性）。
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path), timeout=CONNECTION_TIMEOUT)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if WAL_MODE_ENABLED:
        connection.execute("PRAGMA journal_mode = WAL")

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

    并发保护：UPDATE 的 WHERE 条件带上「读取时的原状态」（乐观锁）。
    若本函数读取之后、写入之前该状态已被其他连接改过，WHERE 不再匹配、
    实际更新行数为 0，此时抛 ConcurrentUpdateError，绝不静默覆盖他人结果。
    注意：status 的业务语义不变——urgent / overdue 仍是由 deadline 推导出的
    提醒级别，不是风险等级（风险等级见 risk_level / models.RISK_LEVELS）。

    返回：更新后的完整记录。
    异常：
        ValueError            状态非法；
        TaskNotFoundError     任务不存在；
        ConcurrentUpdateError 期间状态被其他操作修改。
    """
    _validate_status(status, field="status")

    timestamp = now_iso()
    with get_connection(db_path) as connection:
        existing = _fetch_task(connection, task_id)
        if existing is None:
            raise TaskNotFoundError(f"任务不存在：id={task_id}")

        previous_status = existing["status"]
        cursor = connection.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status IS ?",
            (status, timestamp, int(task_id), previous_status),
        )
        if cursor.rowcount == 0:
            raise ConcurrentUpdateError(
                f"任务 #{task_id} 的状态已被其他操作修改"
                f"（期望 {previous_status!r}），本次更新已取消，请重新读取后再试。"
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


def update_risk_level(
    task_id: int,
    risk_level: str,
    operator: str = models.DEFAULT_OPERATOR,
    remark: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """更新任务风险等级（risk_level），并自动写入一条 escalate 日志。

    用途：v2.0「提级督办（ESCALATE）」动作的唯一执行入口，
    例：update_risk_level(1, "red", operator="张老师", remark="逾期 2 天未推进")
        自动新增日志：action="escalate", operator="张老师", remark="逾期 2 天未推进"

    严格边界：
    1. 只修改 risk_level 一个字段，绝不触碰 owner / department / deadline /
       title / status；
    2. risk_level 只能取 models.RISK_LEVELS（green / yellow / red），
       它与「由 deadline 推导的 status.urgent / status.overdue」是两个独立概念，
       不可混用；
    3. UPDATE 与日志写入在同一事务内（由 get_connection 统一提交或回滚），
       不会出现「改了数据却没留痕」或「留了痕却没改数据」；
    4. 乐观锁：UPDATE 的 WHERE 条件带上「读取时的原 risk_level」，
       期间被其他连接改过则实际更新行数为 0，抛 ConcurrentUpdateError。

    说明：按 v2.0 事件契约，本函数写入的日志 action 固定为 models.ACTION_ESCALATE
    （即 "escalate"），与 risk_level 的具体目标值无关。
    analyze / confirm 等其余事件由后续处置流程自行写入，本函数不代劳。

    返回：更新后的完整记录。
    异常：
        ValueError            风险等级非法；
        TaskNotFoundError     任务不存在；
        ConcurrentUpdateError 期间风险等级被其他操作修改。
    """
    _validate_risk_level(risk_level, field="risk_level")

    timestamp = now_iso()
    with get_connection(db_path) as connection:
        existing = _fetch_task(connection, task_id)
        if existing is None:
            raise TaskNotFoundError(f"任务不存在：id={task_id}")

        previous_risk = existing["risk_level"]
        cursor = connection.execute(
            "UPDATE tasks SET risk_level = ?, updated_at = ? WHERE id = ? AND risk_level IS ?",
            (risk_level, timestamp, int(task_id), previous_risk),
        )
        if cursor.rowcount == 0:
            raise ConcurrentUpdateError(
                f"任务 #{task_id} 的风险等级已被其他操作修改"
                f"（期望 {previous_risk!r}），本次提级已取消，请重新读取后再试。"
            )
        _insert_log(
            connection,
            task_id=int(task_id),
            action=models.ACTION_ESCALATE,
            operator=operator,
            remark=remark,
        )
        updated = _fetch_task(connection, task_id)

    return _row_to_dict(updated)


# 允许人工修正的任务字段（白名单，封闭集合）
#
# 为什么需要这个入口（v3.0 第一阶段）：
#   下发前检查会指出「负责人 / 截止时间 / 执行要求」等关键信息缺失。
#   若任务在第二阶段（识别 → 确认）时未补齐，管理者必须在督办页当场补齐，
#   否则任务将永远无法下发。原先只有 update_status / update_risk_level，
#   无法修正业务字段，导致「要求补充」在界面上无处落地。
EDITABLE_TASK_FIELDS: tuple[str, ...] = (
    "title",
    "department",
    "owner",
    "deadline",
    "priority",
    "description",
)


def update_task_fields(
    task_id: int,
    fields: Mapping[str, Any],
    operator: str = models.DEFAULT_OPERATOR,
    remark: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """人工修正任务的可编辑字段（标题/部门/负责人/截止/优先级/说明）。

    设计边界：
    1. 只允许 EDITABLE_TASK_FIELDS 白名单内的字段，未知字段直接报错；
    2. 空字符串统一视为「清空」（存 NULL），便于管理者把错误信息清掉；
    3. 与 update_status / update_risk_level 一致：同一事务内写入
       task_logs 留痕，并使用乐观锁（updated_at）防止覆盖他人修改；
    4. 日志动作用 models.ACTION_CONFIRM（人工确认/修正），
       因为 models.LOG_ACTIONS 是封闭清单，扩展新动作会破坏既有契约测试。
       具体改了哪些字段写在 remark 里，保证可审计。

    返回：更新后的完整记录。
    异常：TypeError  fields 不是映射；
         ValueError 含未知字段 / 标题被清空 / 优先级或截止时间非法；
         TaskNotFoundError 任务不存在；
         ConcurrentUpdateError 期间任务被其他操作修改。
    """
    if not isinstance(fields, Mapping):
        raise TypeError(f"fields 必须是映射类型，收到：{type(fields).__name__}")

    unknown = [key for key in fields if key not in EDITABLE_TASK_FIELDS]
    if unknown:
        raise ValueError(
            f"不允许修改的字段：{'/'.join(sorted(map(str, unknown)))}"
            f"（允许值：{'/'.join(EDITABLE_TASK_FIELDS)}）"
        )
    if not fields:
        raise ValueError("fields 不能为空，至少需要提供一个待修改字段。")

    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "deadline":
            updates[key] = _normalize_deadline(value)
        elif key == "priority":
            text = _clean_text(value)
            if text is not None and text not in models.PRIORITIES:
                raise ValueError(
                    f"优先级非法：{text}（允许值：{'/'.join(models.PRIORITIES)}）"
                )
            updates[key] = text
        else:
            updates[key] = _clean_text(value)

    # 标题是必填项：允许修正，但不允许清空
    if "title" in updates and not updates["title"]:
        raise ValueError("任务标题不能为空。")

    timestamp = now_iso()
    columns = ", ".join(f"{key} = ?" for key in updates)
    values: list[Any] = [*updates.values(), timestamp]

    with get_connection(db_path) as connection:
        existing = _fetch_task(connection, task_id)
        if existing is None:
            raise TaskNotFoundError(f"任务不存在：id={task_id}")

        previous_updated = existing["updated_at"]
        cursor = connection.execute(
            f"UPDATE {models.TABLE_TASKS} SET {columns}, updated_at = ? "
            "WHERE id = ? AND updated_at IS ?",
            (*values, int(task_id), previous_updated),
        )
        if cursor.rowcount == 0:
            raise ConcurrentUpdateError(
                f"任务 #{task_id} 已被其他操作修改，本次信息修正已取消，请重新读取后再试。"
            )

        _insert_log(
            connection,
            task_id=int(task_id),
            action=models.ACTION_CONFIRM,
            operator=operator,
            remark=remark or "人工修正任务信息",
        )
        updated = _fetch_task(connection, task_id)

    return _row_to_dict(updated)


def append_log(
    task_id: int,
    action: str,
    operator: str = models.DEFAULT_OPERATOR,
    remark: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """追加一条任务操作日志，**不修改 tasks 表任何字段**。

    用途：v2.0「事务事件记忆」的写入口。create / 状态变更 / escalate 三类日志
    已有各自的专用接口（create_task、update_status、update_risk_level），
    而 analyze / draft / request / confirm / reject 这类**不改动任务数据**的
    事件，需要一个独立的追加入口来留痕。

    与既有写接口的关键差异：
    1. **不触碰 tasks 表**——不修改任何业务字段，也不刷新 updated_at。
       「发生了一件事」不等于「任务数据变了」，两者必须分开记账。
    2. action 必须属于 models.LOG_ACTIONS（状态值 + 事务事件），否则直接报错。
    3. 任务不存在时抛 TaskNotFoundError，绝不留下孤儿子键日志。

    返回：刚写入的那条日志记录。
    异常：ValueError action 非法；TaskNotFoundError 任务不存在。
    """
    _validate_log_action(action)

    with get_connection(db_path) as connection:
        existing = _fetch_task(connection, task_id)
        if existing is None:
            raise TaskNotFoundError(f"任务不存在：id={task_id}")

        _insert_log(
            connection,
            task_id=int(task_id),
            action=action,
            operator=operator,
            remark=remark,
        )
        row = connection.execute(
            f"SELECT {', '.join(models.TASK_LOG_COLUMNS)} FROM {models.TABLE_TASK_LOGS} "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (int(task_id),),
        ).fetchone()

    return dict(row)


def append_chain_log(
    task_id: int,
    action: str,
    operator: str = models.DEFAULT_OPERATOR,
    remark: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """追加一条 v2.1 执行链路日志（dispatch / submit）。

    与 append_log 的区别（刻意保持两者并存，互不影响）：
    - append_log 校验 models.LOG_ACTIONS，那是 v2.0 的事务事件契约，
      改动它会破坏既有测试，因此**保持原样、一行未动**；
    - 本函数校验 models.CHAIN_ACTIONS —— 一个只有 dispatch / submit 的
      封闭清单，用于「任务下发」「工作回执」这类执行闭环事件。
      同样写入 task_logs，复用既有日志表与外键、事务、WAL 等全部机制。

    为什么不并入 EVENT_ACTIONS：
      EVENT_ACTIONS 的取值与顺序被 test_database.py / test_execution_tool.py
        逐值断言，一旦扩充会让这些既有测试失败。执行闭环事件与智能体处置事件
        本就是两个维度，单列清单既满足扩展需求，又不破坏 2.0 契约。

    边界：
    1. **不修改 tasks 表任何字段**，连 updated_at 都不刷新——
       「发生了一件事」不等于「任务数据变了」，与 append_log 保持同一口径；
    2. action 必须是 CHAIN_ACTIONS 之一，封闭清单不接受任意外来字符串；
    3. 任务不存在时抛 TaskNotFoundError，绝不留下孤儿子记录。

    返回：刚写入的那条日志记录。
    异常：ValueError action 非法；TaskNotFoundError 任务不存在。
    """
    if action not in models.CHAIN_ACTIONS:
        raise ValueError(
            f"action 非法：{action!r}（允许值：{'/'.join(models.CHAIN_ACTIONS)}）"
        )

    with get_connection(db_path) as connection:
        existing = _fetch_task(connection, task_id)
        if existing is None:
            raise TaskNotFoundError(f"任务不存在：id={task_id}")

        _insert_log(
            connection,
            task_id=int(task_id),
            action=action,
            operator=operator,
            remark=remark,
        )
        row = connection.execute(
            f"SELECT {', '.join(models.TASK_LOG_COLUMNS)} FROM {models.TABLE_TASK_LOGS} "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (int(task_id),),
        ).fetchone()

    return dict(row)


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
    *,
    newest_first: bool = False,
) -> list[dict[str, Any]]:
    """查询任务列表，支持按状态、部门筛选。

    两个条件同时给出时取交集；均为空则返回全部任务。
    排序：
        newest_first=False（默认，保持既有契约）—— 按入库顺序（id 升序）；
        newest_first=True —— 按最新创建优先（id 降序），供「任务督办中心」等
        展示场景使用。两种排序都返回同一批任务，只改变展示顺序，不影响任何业务数据。
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
    sql += " ORDER BY id DESC" if newest_first else " ORDER BY id ASC"

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


def _validate_risk_level(risk_level: Any, field: str = "risk_level") -> None:
    """校验风险等级取值，非法直接报错。

    风险等级只允许 models.RISK_LEVELS（green / yellow / red）。
    特别提醒：status 中的 urgent / overdue 是「由 deadline 推导的提醒级别」，
    不是风险等级，不允许传入这里。
    """
    if risk_level not in models.RISK_LEVELS:
        raise ValueError(
            f"{field} 非法：{risk_level!r}（允许值：{'/'.join(models.RISK_LEVELS)}）"
        )


def _validate_log_action(action: Any, field: str = "action") -> None:
    """校验日志动作取值，非法直接报错。

    允许集合 models.LOG_ACTIONS = 新建 + 全部状态值 + 全部事务事件。
    这样可以防止调用方把自由文本写进 action 列，污染事件链的可审计性。
    """
    if action not in models.LOG_ACTIONS:
        raise ValueError(
            f"{field} 非法：{action!r}（允许值：{'/'.join(models.LOG_ACTIONS)}）"
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
