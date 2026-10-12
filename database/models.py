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

# --- 结果核验与闭环动作（v2.2）：同样只写 task_logs，不并入 EVENT_ACTIONS ---
ACTION_VERIFY: str = "verify"                  # 结果核验
ACTION_NEED_SUPPLEMENT: str = "need_supplement"  # 核验不通过：要求补充材料
ACTION_REJECT_REWORK: str = "reject_rework"      # 核验不通过：驳回重办
ACTION_CLOSE: str = "close"                    # 核验通过，任务正式闭环

# 允许写入 task_logs 的执行链路动作（封闭清单，不接受任意外来字符串）
CHAIN_ACTIONS: tuple[str, ...] = (
    ACTION_DISPATCH,
    ACTION_SUBMIT,
    ACTION_VERIFY,
    ACTION_NEED_SUPPLEMENT,
    ACTION_REJECT_REWORK,
    ACTION_CLOSE,
)
# 全部可落库的日志动作（v2.0 契约 + v2.1/v2.2 执行链路），供 UI 注册中文标签时参照
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
# ============================================================
# v2.2：提交内容指纹（核验绑定到具体版本）
# ============================================================
#
# task_submissions 对 task_id 是 UNIQUE + upsert，同一行会被新内容覆盖，
# submission_id 永不变化。仅凭 submission_id 无法区分「已核验的是哪一版内容」
# ——旧版本的 approved 会被误套用到新版本上。
# 用内容指纹而不是时间戳：同一秒内两次提交时间戳可能相同，且时间戳可被测试注入。
VERIFICATION_SUBMISSION_DIGEST: str = "submission_digest"

# ============================================================
# v2.2：正式闭环记录（task_closures）
# ============================================================
#
# 为什么不写进 tasks.status：
#   1. tasks.status 六个取值无闭环态，新增需 ALTER TABLE 迁移；
#   2. tasks 字段顺序被 5 处既有测试逐值锁定；
#   3. 闭环是独立于执行状态的业务事实，塞进 status 会污染 reminder_agent
#      排除集、report_agent 完成率与 submission_check 判定。
# 一任务至多一次闭环由 task_id UNIQUE 保证。
TABLE_TASK_CLOSURES: str = "task_closures"

TASK_CLOSURE_COLUMNS: tuple[str, ...] = (
    "task_id", "submission_id", "submission_digest", "verification_id",
    "closed_by", "closed_at", "closure_note", "created_at",
)
TASK_CLOSURE_ALL_COLUMNS: tuple[str, ...] = ("id",) + TASK_CLOSURE_COLUMNS

CREATE_TASK_CLOSURES_SQL: str = """
CREATE TABLE IF NOT EXISTS task_closures (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           INTEGER NOT NULL UNIQUE,
    submission_id     INTEGER,
    submission_digest TEXT,
    verification_id   INTEGER NOT NULL,
    closed_by         TEXT    NOT NULL,
    closed_at         TEXT    NOT NULL,
    closure_note      TEXT,
    created_at        TEXT    NOT NULL,
    FOREIGN KEY (task_id)       REFERENCES tasks (id)           ON DELETE CASCADE,
    FOREIGN KEY (submission_id) REFERENCES task_submissions (id) ON DELETE SET NULL
)
"""

# ============================================================
# v2.2 结果核验（verifications）
# ============================================================
# 一任务可有多条核验记录（驳回→补充→再核验的轨迹要留得住），
# 因此 task_id **不加 UNIQUE**。
TABLE_VERIFICATIONS: str = "verifications"
VERIFICATION_PENDING: str = "pending"
VERIFICATION_APPROVED: str = "approved"
VERIFICATION_NEED_SUPPLEMENT: str = "need_supplement"
VERIFICATION_REJECTED: str = "rejected"
VERIFICATION_MANUAL_REVIEW: str = "manual_review"
VERIFICATION_STATUSES: tuple[str, ...] = (
    VERIFICATION_PENDING, VERIFICATION_APPROVED, VERIFICATION_NEED_SUPPLEMENT,
    VERIFICATION_REJECTED, VERIFICATION_MANUAL_REVIEW,
)
VERIFICATION_COLUMNS: tuple[str, ...] = (
    "task_id", "submission_id", "submission_digest", "status", "reason", "requirement_match",
    "deliverable_complete", "process_valid", "state_consistent",
    "verified_by", "verified_at",
)
VERIFICATION_ALL_COLUMNS: tuple[str, ...] = ("id",) + VERIFICATION_COLUMNS
VERIFICATION_SUBMISSION_DIGEST: str = "submission_digest"

CREATE_VERIFICATIONS_SQL: str = """
CREATE TABLE IF NOT EXISTS verifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL,
    submission_id       INTEGER,
    submission_digest   TEXT,
    status              TEXT    NOT NULL,
    reason              TEXT,
    requirement_match   INTEGER NOT NULL DEFAULT 0,
    deliverable_complete INTEGER NOT NULL DEFAULT 0,
    process_valid       INTEGER NOT NULL DEFAULT 0,
    state_consistent    INTEGER NOT NULL DEFAULT 0,
    verified_by         TEXT,
    verified_at         TEXT    NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE,
    FOREIGN KEY (submission_id) REFERENCES task_submissions (id) ON DELETE SET NULL
)
"""

CREATE_VERIFICATION_INDEXES_SQL: str = """
CREATE INDEX IF NOT EXISTS idx_verifications_task ON verifications (task_id)
"""
# ============================================================
# v2.2 任务办理要求与办结标准（task_metadata）
# ============================================================
# 一任务至多一条（1:1），故 task_id 用 UNIQUE。
# 不用 ALTER TABLE 加到 tasks 上：既有表 CREATE IF NOT EXISTS 是空操作，
# 且 tasks 字段顺序被 5 处既有测试逐值锁定。
TABLE_TASK_METADATA: str = "task_metadata"
CLOSURE_SOURCE_MATERIAL: str = "material"
CLOSURE_SOURCE_RULE: str = "rule"
CLOSURE_SOURCE_AI: str = "ai"
CLOSURE_SOURCE_MANUAL: str = "manual"
CLOSURE_SOURCES: tuple[str, ...] = (
    CLOSURE_SOURCE_MATERIAL, CLOSURE_SOURCE_RULE,
    CLOSURE_SOURCE_AI, CLOSURE_SOURCE_MANUAL,
)
TASK_METADATA_COLUMNS: tuple[str, ...] = (
    "task_id", "execution_requirements", "closure_criteria",
    "closure_source", "closure_confirmed", "created_at", "updated_at",
)
TASK_METADATA_ALL_COLUMNS: tuple[str, ...] = ("id",) + TASK_METADATA_COLUMNS

CREATE_TASK_METADATA_SQL: str = """
CREATE TABLE IF NOT EXISTS task_metadata (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id               INTEGER NOT NULL UNIQUE,
    execution_requirements TEXT,
    closure_criteria      TEXT,
    closure_source        TEXT,
    closure_confirmed     INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
)
"""
# ============================================================
# v3.0 第二阶段：执行反馈（task_feedback）
# ============================================================
#
# 为什么新增一张表，而不是改造 task_submissions：
#   task_submissions 是「办理结果回执」，被核验与正式闭环通过内容指纹
#   （submission_digest）强绑定，一个任务只有一行（UNIQUE + upsert）。
#   如果改成多行，verification_store / closure_service 的版本绑定会整体失效。
#
#   而执行期反馈是另一件业务事实：管理者在任务执行期间可以**多次**录入
#   「已完成 / 进行中」，需要保留完整轨迹，并能明确取到「最新有效反馈」。
#   因此单列一张**追加式**的表，两个业务事实各自独立、互不覆盖：
#       task_submissions  我交了什么（核验/闭环的锚点，一任务一行）
#       task_feedback     执行进展如何（追加式，最新一条为当前有效反馈）
TABLE_TASK_FEEDBACK: str = "task_feedback"

# 反馈状态：只要两种（比赛版只有管理者代录）
FEEDBACK_IN_PROGRESS: str = "in_progress"   # 进行中 / 暂未完成
FEEDBACK_COMPLETED: str = "completed"       # 已完成
FEEDBACK_STATUSES: tuple[str, ...] = (FEEDBACK_IN_PROGRESS, FEEDBACK_COMPLETED)

TASK_FEEDBACK_COLUMNS: tuple[str, ...] = (
    "task_id", "status", "note", "file_path", "operator", "created_at",
)
TASK_FEEDBACK_ALL_COLUMNS: tuple[str, ...] = ("id",) + TASK_FEEDBACK_COLUMNS

CREATE_TASK_FEEDBACK_SQL: str = """
CREATE TABLE IF NOT EXISTS task_feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    status     TEXT    NOT NULL,
    note       TEXT,
    file_path  TEXT,
    operator   TEXT,
    created_at TEXT    NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
)
"""

CREATE_FEEDBACK_INDEXES_SQL: str = (
    "CREATE INDEX IF NOT EXISTS idx_task_feedback_task ON task_feedback (task_id)"
)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TASKS_SQL,
    CREATE_TASK_LOGS_SQL,
    CREATE_TASK_SUBMISSIONS_SQL,
    CREATE_VERIFICATIONS_SQL,
    CREATE_TASK_METADATA_SQL,
    CREATE_TASK_CLOSURES_SQL,
    CREATE_TASK_FEEDBACK_SQL,
) + CREATE_INDEXES_SQL + (CREATE_VERIFICATION_INDEXES_SQL, CREATE_FEEDBACK_INDEXES_SQL)
