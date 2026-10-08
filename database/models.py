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
                      / 事务事件（analyze/draft/request/confirm/reject/escalate）
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

# --- 2.1 状态类动作 ---

# 新建任务时的动作名
ACTION_CREATE: str = "create"

# 状态类日志动作 = 新建 + 全部状态值（状态变更时 action 即为新状态）
LOG_ACTIONS_STATUS: tuple[str, ...] = (ACTION_CREATE,) + TASK_STATUSES


# --- 2.2 事务事件动作（v2.0）---

# 说明：任务状态（pending/confirmed/...）与风险等级（green/yellow/red）是两个
# 不同概念。status 中的 urgent / overdue 恒由 deadline 推导，不得当作风险等级使用。
# 下列动作用于记录「事务事件记忆」——处置链路上的分析、建议、确认与执行，
# 它们既不是任务状态，也不是风险等级，因此独立成组、绝不并入 TASK_STATUSES。
ACTION_ANALYZE: str = "analyze"    # 完成一次处置分析
ACTION_DRAFT: str = "draft"        # 生成提醒 / 督办文案草稿
ACTION_REQUEST: str = "request"    # 请求人工补充缺失信息
ACTION_CONFIRM: str = "confirm"    # 人工确认处置方案
ACTION_REJECT: str = "reject"      # 人工否决处置方案
ACTION_ESCALATE: str = "escalate"  # 提级督办（risk_level 提升为 red）

# 事务事件动作集合
EVENT_ACTIONS: tuple[str, ...] = (
    ACTION_ANALYZE,
    ACTION_DRAFT,
    ACTION_REQUEST,
    ACTION_CONFIRM,
    ACTION_REJECT,
    ACTION_ESCALATE,
)

# 允许出现的全部日志动作 = 新建 + 全部状态值 + 全部事务事件
# 说明：task_logs.action 为无 CHECK 约束的 TEXT 列（见 CREATE_TASK_LOGS_SQL），
# 因此新增动作属于 Python 层契约变更，不需要数据库迁移。
LOG_ACTIONS: tuple[str, ...] = LOG_ACTIONS_STATUS + EVENT_ACTIONS

# --- 2.3 执行链路动作（v2.1）---
#
# 为什么 dispatch / submit 不并入 EVENT_ACTIONS：
#   EVENT_ACTIONS 是 v2.0 已固化的「事务事件」契约，其取值与构成顺序被
#   test_database.py / test_execution_tool.py 逐值断言。往里加动作会让这些
#   既有测试全部失败，属于破坏 2.0 稳定性。
#   而 v2.1 的「任务下发」「工作回执」确实属于另一类事件——它们描述的是
#   **执行闭环的推进**，不是智能体对任务本身的处置。
#
# 因此这里单列一个封闭清单 CHAIN_ACTIONS：同样写进 task_logs（复用既有日志表，
# 不新建事件体系），但独立于 v2.0 契约之外。写入入口见 sqlite_db.append_chain_log()。
ACTION_DISPATCH: str = "dispatch"   # 系统内任务下发
ACTION_SUBMIT: str = "submit"      # 负责人提交工作回执

# 允许写入 task_logs 的执行链路动作（封闭清单，不接受任意外来字符串）
CHAIN_ACTIONS: tuple[str, ...] = (
    ACTION_DISPATCH,
    ACTION_SUBMIT,
)

# 全部可落库的日志动作（v2.0 契约 + v2.1 执行链路），供 UI 注册中文标签时参照
ALL_LOG_ACTIONS: tuple[str, ...] = LOG_ACTIONS + CHAIN_ACTIONS

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

# ============================================================
# 五、v2.1：工作回执（task_submissions）
# ============================================================
#
# 业务前提：**一项任务只有一个负责人、一条回执**。
# 因此不建多人 Assignment 表，直接在 task_id 上加 UNIQUE 约束，
# 让「一个任务至多一条回执」由数据库本身保证，而不是靠应用层自觉。
#
# 兼容性：CREATE TABLE IF NOT EXISTS，既有 .db 文件下次连接时自动补建，
# 不需要任何迁移脚本，也不触碰 2.0 已有的 tasks / task_logs 结构与数据。

TABLE_TASK_SUBMISSIONS: str = "task_submissions"

# 回执状态：
#   submitted 已提交
# 待提交 / 已逾期未提交 **不入库**——它们是「没有回执记录」时由 deadline
# 推导出来的展示态（见 tools/submission_check.py），数据库只如实记录
# 「确实存在一条已提交的回执」这一事实，避免存一份会过期的冗余状态。
SUBMISSION_SUBMITTED: str = "submitted"
SUBMISSION_STATUSES: tuple[str, ...] = (SUBMISSION_SUBMITTED,)

# 字段顺序即 PRAGMA table_info 的顺序
SUBMISSION_COLUMNS: tuple[str, ...] = (
    "task_id",
    "content",
    "file_path",
    "status",
    "submitted_at",
    "created_at",
    "updated_at",
)
SUBMISSION_ALL_COLUMNS: tuple[str, ...] = ("id",) + SUBMISSION_COLUMNS

CREATE_TASK_SUBMISSIONS_SQL: str = """
CREATE TABLE IF NOT EXISTS task_submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL UNIQUE,
    content      TEXT    NOT NULL,
    file_path    TEXT,
    status       TEXT    NOT NULL DEFAULT 'submitted',
    submitted_at TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
)
"""

# 建表语句集合，供 init_db() 按顺序执行
# 说明：v2.1 的 task_submissions 追加在 v2.0 两张表之后，
# CREATE_TASKS_SQL 与 CREATE_TASK_LOGS_SQL 一字未改，既有数据库文件无需迁移。
SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TASKS_SQL,
    CREATE_TASK_LOGS_SQL,
    CREATE_TASK_SUBMISSIONS_SQL,
) + CREATE_INDEXES_SQL
