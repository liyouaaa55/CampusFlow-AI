"""数据表结构定义（Agent Memory 的物理模型）。

本文件只描述「表结构与字段约束」，不含任何读写逻辑。

表一：tasks（任务主表）
    id           INTEGER PRIMARY KEY AUTOINCREMENT
    title        TEXT      任务标题
    department   TEXT      责任部门
    owner        TEXT      负责人
    deadline     TEXT      截止日期（YYYY-MM-DD）
    priority     TEXT      high / medium / low
    status       TEXT      pending / confirmed / processing / urgent / completed / overdue
    risk_level   TEXT      green / yellow / red（PlannerAgent 产出）
    need_confirm INTEGER   是否需要人工确认（0 / 1）
    description  TEXT      事项具体要求
    created_at   TEXT      创建时间
    updated_at   TEXT      最后更新时间

表二：task_logs（操作日志表，Agent 的记忆轨迹）
    id         INTEGER PRIMARY KEY AUTOINCREMENT
    task_id    INTEGER  关联 tasks.id
    action     TEXT     create / 变更后的状态值（如 completed）
    operator   TEXT     操作人（AI 或人工姓名）
    remark     TEXT     备注
    created_at TEXT     操作时间

说明：第四阶段的需求以本文件为准，因此不再保留第一阶段的 source_type 字段。
"""

from __future__ import annotations

from config import settings

# ============================================================
# 一、枚举（直接复用全局配置，保证数据库与业务层完全一致）
# ============================================================

# 任务状态：仅允许 6 种
TASK_STATUSES: tuple[str, ...] = settings.TASK_STATUSES
STATUS_LABELS: dict[str, str] = settings.STATUS_LABELS

# 优先级：仅允许 3 级
PRIORITIES: tuple[str, ...] = settings.PRIORITIES

# 风险等级（与 PlannerAgent 的输出保持一致）
RISK_LEVELS: tuple[str, ...] = ("green", "yellow", "red")

# 默认值
DEFAULT_STATUS: str = settings.DEFAULT_STATUS
DEFAULT_PRIORITY: str = settings.DEFAULT_PRIORITY
DEFAULT_OPERATOR: str = "AI"

# ============================================================
# 二、日志动作
# ============================================================

# 新建任务时的动作名
ACTION_CREATE: str = "create"

# 允许出现的日志动作 = 新建 + 全部状态值（状态变更时 action 即为新状态）
LOG_ACTIONS: tuple[str, ...] = (ACTION_CREATE,) + TASK_STATUSES

# ============================================================
# 三、表名与字段顺序（字段顺序即 PRAGMA table_info 的顺序）
# ============================================================

TABLE_TASKS: str = "tasks"
TABLE_TASK_LOGS: str = "task_logs"

# tasks 表字段（不含自增主键），顺序与需求文档一致
TASK_COLUMNS: tuple[str, ...] = (
    "title",
    "department",
    "owner",
    "deadline",
    "priority",
    "status",
    "risk_level",
    "need_confirm",
    "description",
    "created_at",
    "updated_at",
)

# tasks 表完整字段（含主键）
TASK_ALL_COLUMNS: tuple[str, ...] = ("id",) + TASK_COLUMNS

# task_logs 表完整字段（含主键）
TASK_LOG_COLUMNS: tuple[str, ...] = (
    "id",
    "task_id",
    "action",
    "operator",
    "remark",
    "created_at",
)

# ============================================================
# 四、DDL 建表语句
# ============================================================

CREATE_TASKS_SQL: str = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    department   TEXT,
    owner        TEXT,
    deadline     TEXT,
    priority     TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    risk_level   TEXT,
    need_confirm INTEGER NOT NULL DEFAULT 0,
    description  TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
)
"""

CREATE_TASK_LOGS_SQL: str = """
CREATE TABLE IF NOT EXISTS task_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    action     TEXT    NOT NULL,
    operator   TEXT    NOT NULL DEFAULT 'AI',
    remark     TEXT,
    created_at TEXT    NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
)
"""

# 常用查询的索引：状态筛选、部门筛选、按任务查日志
CREATE_INDEXES_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_department ON tasks (department)",
    "CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs (task_id)",
)

# 建表语句集合，供 init_db() 按顺序执行
SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TASKS_SQL,
    CREATE_TASK_LOGS_SQL,
) + CREATE_INDEXES_SQL
