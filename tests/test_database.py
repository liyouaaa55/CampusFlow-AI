"""database 层（Agent Memory）单元测试。

测试策略：每个测试使用 tmp_path 下的独立数据库文件，互不干扰，
不触碰真实的 data/campusflow.db。所有任务数据均为演示数据（Synthetic Data）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agent import planner_agent
from config import settings
from database import models, sqlite_db

# 固定基准日期，保证 PlannerAgent 输出稳定（与第三阶段测试一致）
TODAY = "2026-09-23"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """创建并初始化一个临时数据库，返回其路径。"""
    db_path = tmp_path / "campusflow_test.db"
    sqlite_db.init_db(db_path)
    return db_path


def sample_task(**overrides: object) -> dict:
    """构造一条标准的任务数据（默认字段齐备）。"""
    task = {
        "title": "举办机器人创新周",
        "department": "学工办",
        "owner": "张老师",
        "deadline": "2026-09-30",
        "priority": "medium",
        "status": "pending",
        "risk_level": "green",
        "need_confirm": False,
        "description": "制定活动方案",
    }
    task.update(overrides)
    return task


def table_columns(db_path: Path, table: str) -> list[str]:
    """读取某张表的字段名（按定义顺序）。"""
    with sqlite_db.get_connection(db_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in rows]


def sqlite_objects(db_path: Path, kind: str) -> list[str]:
    """列出数据库中的对象名（table / index）。"""
    with sqlite_db.get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? ORDER BY name", (kind,)
        ).fetchall()
    return [row["name"] for row in rows]


# ============================================================
# 一、建库
# ============================================================


def test_init_db_creates_file(db: Path) -> None:
    """init_db 返回数据库路径并创建文件。"""
    assert db.exists()
    assert sqlite_db.init_db(db) == db


def test_init_db_is_idempotent(db: Path) -> None:
    """重复初始化不报错，也不会重复建表。"""
    sqlite_db.init_db(db)
    sqlite_db.init_db(db)
    assert "tasks" in sqlite_objects(db, "table")
    assert sqlite_objects(db, "table").count("tasks") == 1


def test_init_db_creates_required_tables(db: Path) -> None:
    """必须存在 tasks 与 task_logs 两张表。"""
    tables = sqlite_objects(db, "table")
    assert models.TABLE_TASKS in tables
    assert models.TABLE_TASK_LOGS in tables


def test_tasks_table_columns_match_spec(db: Path) -> None:
    """tasks 表字段与顺序必须与需求文档一致。"""
    assert table_columns(db, models.TABLE_TASKS) == list(models.TASK_ALL_COLUMNS)


def test_task_logs_table_columns_match_spec(db: Path) -> None:
    """task_logs 表字段与顺序必须与需求文档一致。"""
    assert table_columns(db, models.TABLE_TASK_LOGS) == list(models.TASK_LOG_COLUMNS)


def test_indexes_created(db: Path) -> None:
    """状态与部门筛选依赖索引，应随建表一起创建。"""
    indexes = sqlite_objects(db, "index")
    assert "idx_tasks_status" in indexes
    assert "idx_tasks_department" in indexes
    assert "idx_task_logs_task_id" in indexes


def test_models_constants_mirror_settings() -> None:
    """表结构里的枚举必须与全局配置保持一致，避免两处定义漂移。"""
    assert models.TASK_STATUSES == settings.TASK_STATUSES
    assert models.PRIORITIES == settings.PRIORITIES
    assert models.DEFAULT_STATUS == settings.DEFAULT_STATUS
    assert models.LOG_ACTIONS == (models.ACTION_CREATE,) + settings.TASK_STATUSES


# ============================================================
# 二、新增单条任务
# ============================================================


def test_create_task_returns_record_with_id(db: Path) -> None:
    """新增后返回带自增 id 的完整记录。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    assert isinstance(created["id"], int) and created["id"] > 0
    assert created["title"] == "举办机器人创新周"


def test_create_task_applies_defaults(db: Path) -> None:
    """缺省字段使用默认值：状态 pending、need_confirm 0、其余为 None。"""
    created = sqlite_db.create_task({"title": "整理档案"}, db_path=db)
    assert created["status"] == models.DEFAULT_STATUS
    assert created["priority"] is None
    assert created["risk_level"] is None
    assert created["need_confirm"] is False
    assert created["department"] is None
    assert created["description"] is None


def test_create_task_cleans_fields(db: Path) -> None:
    """文本字段去首尾空白，空字符串按缺失处理。"""
    created = sqlite_db.create_task(
        sample_task(owner="  张老师  ", department="", description="   "), db_path=db
    )
    assert created["owner"] == "张老师"
    assert created["department"] is None
    assert created["description"] is None


def test_create_task_ignores_unknown_and_readonly_keys(db: Path) -> None:
    """未知字段与只读字段被忽略，便于 PlannerAgent 输出直接入库。"""
    task = sample_task(suggestions=["负责人缺失，请补充负责人"], id=999, created_at="被忽略")
    created = sqlite_db.create_task(task, db_path=db)
    assert created["id"] != 999
    assert "suggestions" not in created


@pytest.mark.parametrize("title", [None, "", "   "])
def test_create_task_requires_title(db: Path, title: object) -> None:
    """标题为空时拒绝写入。"""
    with pytest.raises(ValueError):
        sqlite_db.create_task(sample_task(title=title), db_path=db)


def test_create_task_rejects_non_dict(db: Path) -> None:
    """入参不是字典时抛 TypeError。"""
    with pytest.raises(TypeError):
        sqlite_db.create_task(["not a dict"], db_path=db)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [(None, models.DEFAULT_STATUS), ("", models.DEFAULT_STATUS), ("  ", models.DEFAULT_STATUS)],
)
def test_create_task_status_defaults_when_absent(db: Path, raw_status: object, expected: str) -> None:
    """状态缺失（None / 空字符串）时使用默认状态 pending。"""
    created = sqlite_db.create_task(sample_task(status=raw_status), db_path=db)
    assert created["status"] == expected


@pytest.mark.parametrize("bad_status", ["done", "已完成", "已完成 ", "PENDING"])
def test_create_task_rejects_invalid_status(db: Path, bad_status: str) -> None:
    """状态不在允许范围内时报错（状态值区分大小写，必须是 6 种之一）。"""
    with pytest.raises(ValueError):
        sqlite_db.create_task(sample_task(status=bad_status), db_path=db)


@pytest.mark.parametrize("bad_priority", ["P0", "紧急", "urgent"])
def test_create_task_rejects_invalid_priority(db: Path, bad_priority: str) -> None:
    """优先级只允许 high / medium / low。"""
    with pytest.raises(ValueError):
        sqlite_db.create_task(sample_task(priority=bad_priority), db_path=db)


def test_create_task_rejects_invalid_risk_level(db: Path) -> None:
    """风险等级只允许 green / yellow / red。"""
    with pytest.raises(ValueError):
        sqlite_db.create_task(sample_task(risk_level="blue"), db_path=db)


@pytest.mark.parametrize("bad_deadline", ["2026/09/30", "下周三", "2026-13-01", "20260930"])
def test_create_task_rejects_invalid_deadline(db: Path, bad_deadline: str) -> None:
    """截止日期必须是合法的 YYYY-MM-DD。"""
    with pytest.raises(ValueError):
        sqlite_db.create_task(sample_task(deadline=bad_deadline), db_path=db)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (1, True), ("true", True), ("是", True), (False, False), (0, False), ("no", False), (None, False)],
)
def test_create_task_normalizes_need_confirm(db: Path, raw: object, expected: bool) -> None:
    """need_confirm 兼容布尔、数字与常见文本写法。"""
    created = sqlite_db.create_task(sample_task(need_confirm=raw), db_path=db)
    assert created["need_confirm"] is expected


def test_create_task_rejects_unparsable_need_confirm(db: Path) -> None:
    """无法解析的 need_confirm 直接报错。"""
    with pytest.raises(ValueError):
        sqlite_db.create_task(sample_task(need_confirm="maybe"), db_path=db)


def test_create_task_writes_create_log(db: Path) -> None:
    """新增任务必须自动写入一条 create 日志。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    logs = sqlite_db.get_logs(created["id"], db_path=db)

    assert len(logs) == 1
    assert logs[0]["action"] == models.ACTION_CREATE
    assert logs[0]["operator"] == models.DEFAULT_OPERATOR == "AI"
    assert logs[0]["remark"] == "任务创建"
    assert logs[0]["task_id"] == created["id"]


def test_create_task_accepts_planner_output(db: Path) -> None:
    """PlannerAgent 的输出可以直接入库，规划字段完整保留。"""
    planned = planner_agent.plan(
        [
            {
                "title": "举办机器人创新周",
                "department": "学工办",
                "owner": None,
                "deadline": "2026-09-30",
                "priority": None,
                "status": "pending",
                "description": "制定活动方案",
            }
        ]
    )[0]
    assert planned["need_confirm"] is True  # 负责人与优先级缺失

    created = sqlite_db.create_task(planned, db_path=db)
    assert created["priority"] == "medium"          # 由规则推断
    assert created["risk_level"] == "yellow"
    assert created["need_confirm"] is True
    assert created["owner"] is None
    assert created["status"] == "pending"


# ============================================================
# 三、批量新增
# ============================================================


def test_batch_create_returns_records_in_order(db: Path) -> None:
    """批量新增保持入参顺序，id 递增。"""
    created = sqlite_db.batch_create(
        [sample_task(title="任务一"), sample_task(title="任务二"), sample_task(title="任务三")],
        db_path=db,
    )
    assert [item["title"] for item in created] == ["任务一", "任务二", "任务三"]
    assert created[0]["id"] < created[1]["id"] < created[2]["id"]
    assert len(sqlite_db.list_tasks(db_path=db)) == 3


def test_batch_create_empty_list(db: Path) -> None:
    """空列表直接返回空列表，不产生任何数据。"""
    assert sqlite_db.batch_create([], db_path=db) == []
    assert sqlite_db.list_tasks(db_path=db) == []


def test_batch_create_writes_log_per_task(db: Path) -> None:
    """每条任务各写一条 create 日志。"""
    created = sqlite_db.batch_create([sample_task(), sample_task(title="第二项")], db_path=db)
    for item in created:
        logs = sqlite_db.get_logs(item["id"], db_path=db)
        assert [log["action"] for log in logs] == [models.ACTION_CREATE]


def test_batch_create_is_atomic_on_invalid_item(db: Path) -> None:
    """批量写入是原子的：任一条非法则整体不入库。"""
    tasks = [sample_task(title="合法任务"), sample_task(status="不存在的状态"), sample_task(title="另一个合法任务")]
    with pytest.raises(ValueError):
        sqlite_db.batch_create(tasks, db_path=db)
    assert sqlite_db.list_tasks(db_path=db) == []


def test_batch_create_rejects_non_list(db: Path) -> None:
    """入参必须是列表。"""
    with pytest.raises(TypeError):
        sqlite_db.batch_create(sample_task(), db_path=db)  # type: ignore[arg-type]


def test_batch_create_rejects_non_dict_item(db: Path) -> None:
    """列表中混入非字典元素时报错。"""
    with pytest.raises(TypeError):
        sqlite_db.batch_create([sample_task(), "不是字典"], db_path=db)  # type: ignore[list-item]


# ============================================================
# 四、查询
# ============================================================


def test_get_task_returns_none_when_missing(db: Path) -> None:
    """查询不存在的任务返回 None。"""
    assert sqlite_db.get_task(12345, db_path=db) is None


def test_get_task_roundtrip(db: Path) -> None:
    """写入与读出的字段完全一致。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    fetched = sqlite_db.get_task(created["id"], db_path=db)
    assert fetched == created


def test_list_tasks_returns_all_in_insert_order(db: Path) -> None:
    """未传筛选条件时返回全部任务，按入库顺序排列。"""
    sqlite_db.batch_create(
        [sample_task(title="任务一"), sample_task(title="任务二")], db_path=db
    )
    assert [task["title"] for task in sqlite_db.list_tasks(db_path=db)] == ["任务一", "任务二"]


def test_list_tasks_on_empty_db(db: Path) -> None:
    """空库返回空列表。"""
    assert sqlite_db.list_tasks(db_path=db) == []


def test_list_tasks_need_confirm_is_bool(db: Path) -> None:
    """need_confirm 以布尔值返回，方便前端直接使用。"""
    sqlite_db.create_task(sample_task(need_confirm=True), db_path=db)
    assert sqlite_db.list_tasks(db_path=db)[0]["need_confirm"] is True


def test_list_tasks_returns_standard_columns(db: Path) -> None:
    """返回记录的字段与顺序与表定义一致。"""
    sqlite_db.create_task(sample_task(), db_path=db)
    assert list(sqlite_db.list_tasks(db_path=db)[0].keys()) == list(models.TASK_ALL_COLUMNS)


# ============================================================
# 五、筛选
# ============================================================


@pytest.fixture()
def seeded_db(db: Path) -> Path:
    """预置三条不同部门 / 状态的任务。"""
    sqlite_db.batch_create(
        [
            sample_task(title="宿舍消防隐患排查", department="保卫处", status="pending"),
            sample_task(title="举办机器人创新周", department="学工办", status="processing"),
            sample_task(title="更新宣传栏", department="学工办", status="completed"),
        ],
        db_path=db,
    )
    return db


def test_list_tasks_filter_by_status(seeded_db: Path) -> None:
    """按状态筛选。"""
    tasks = sqlite_db.list_tasks(status="processing", db_path=seeded_db)
    assert [task["title"] for task in tasks] == ["举办机器人创新周"]


def test_list_tasks_filter_by_department(seeded_db: Path) -> None:
    """按部门筛选。"""
    tasks = sqlite_db.list_tasks(department="学工办", db_path=seeded_db)
    assert [task["title"] for task in tasks] == ["举办机器人创新周", "更新宣传栏"]


def test_list_tasks_filter_combined(seeded_db: Path) -> None:
    """状态 + 部门同时筛选时取交集。"""
    tasks = sqlite_db.list_tasks(status="completed", department="学工办", db_path=seeded_db)
    assert [task["title"] for task in tasks] == ["更新宣传栏"]


def test_list_tasks_filter_no_match(seeded_db: Path) -> None:
    """筛选无结果时返回空列表。"""
    assert sqlite_db.list_tasks(department="教务处", db_path=seeded_db) == []


def test_list_tasks_unknown_status_raises(seeded_db: Path) -> None:
    """传入未知状态时报错，避免静默返回空结果。"""
    with pytest.raises(ValueError):
        sqlite_db.list_tasks(status="unknown-status", db_path=seeded_db)


# ============================================================
# 六、状态修改与日志
# ============================================================


def test_update_status_returns_updated_record(db: Path) -> None:
    """状态修改返回更新后的完整记录。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    updated = sqlite_db.update_status(created["id"], "processing", db_path=db)

    assert updated["status"] == "processing"
    assert updated["id"] == created["id"]
    assert sqlite_db.get_task(created["id"], db_path=db)["status"] == "processing"


def test_update_status_writes_log_with_operator_and_remark(db: Path) -> None:
    """需求示例：update_status(1, "completed", operator="李老师", remark="活动已完成")。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    sqlite_db.update_status(
        created["id"], "completed", operator="李老师", remark="活动已完成", db_path=db
    )

    logs = sqlite_db.get_logs(created["id"], db_path=db)
    assert len(logs) == 2
    latest = logs[-1]
    assert latest["action"] == "completed"      # action 即为新状态
    assert latest["operator"] == "李老师"
    assert latest["remark"] == "活动已完成"


def test_update_status_default_operator_is_ai(db: Path) -> None:
    """默认操作人为 AI。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    sqlite_db.update_status(created["id"], "confirmed", db_path=db)
    assert sqlite_db.get_logs(created["id"], db_path=db)[-1]["operator"] == "AI"


def test_update_status_log_sequence(db: Path) -> None:
    """状态多次流转时日志按时间顺序完整保留。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    for status in ("confirmed", "processing", "completed"):
        sqlite_db.update_status(created["id"], status, operator="张老师", db_path=db)

    assert [log["action"] for log in sqlite_db.get_logs(created["id"], db_path=db)] == [
        models.ACTION_CREATE,
        "confirmed",
        "processing",
        "completed",
    ]


def test_update_status_missing_task_raises(db: Path) -> None:
    """任务不存在时报错，不静默失败。"""
    with pytest.raises(sqlite_db.TaskNotFoundError):
        sqlite_db.update_status(999, "completed", db_path=db)


@pytest.mark.parametrize("bad_status", ["done", "已完成", ""])
def test_update_status_rejects_invalid_status(db: Path, bad_status: str) -> None:
    """非法状态拒绝写入，且不产生日志。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    with pytest.raises(ValueError):
        sqlite_db.update_status(created["id"], bad_status, db_path=db)
    assert len(sqlite_db.get_logs(created["id"], db_path=db)) == 1


def test_update_status_keeps_created_at(db: Path) -> None:
    """状态修改不改变 created_at，只会刷新 updated_at。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    updated = sqlite_db.update_status(created["id"], "processing", db_path=db)

    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]


def test_update_status_only_affects_target(db: Path) -> None:
    """只修改目标任务的状态。"""
    first, second = sqlite_db.batch_create(
        [sample_task(title="任务一"), sample_task(title="任务二")], db_path=db
    )
    sqlite_db.update_status(first["id"], "completed", db_path=db)

    assert sqlite_db.get_task(second["id"], db_path=db)["status"] == "pending"
    assert len(sqlite_db.get_logs(second["id"], db_path=db)) == 1


# ============================================================
# 七、日志查询
# ============================================================


def test_get_logs_unknown_task_returns_empty(db: Path) -> None:
    """不存在的任务没有日志，返回空列表。"""
    assert sqlite_db.get_logs(999, db_path=db) == []


def test_get_logs_returns_standard_columns(db: Path) -> None:
    """日志字段与顺序与表定义一致。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    logs = sqlite_db.get_logs(created["id"], db_path=db)
    assert list(logs[0].keys()) == list(models.TASK_LOG_COLUMNS)


def test_log_timestamp_format(db: Path) -> None:
    """日志时间可被解析为时间对象（供 Reminder / Report 排序）。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    log = sqlite_db.get_logs(created["id"], db_path=db)[0]
    assert datetime.fromisoformat(log["created_at"])


def test_logs_removed_with_task(db: Path) -> None:
    """删除任务时其日志一并清理，避免留下孤立记录。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    sqlite_db.delete_task(created["id"], db_path=db)

    assert sqlite_db.get_logs(created["id"], db_path=db) == []
    with sqlite_db.get_connection(db) as connection:
        remaining = connection.execute(
            f"SELECT COUNT(*) AS total FROM {models.TABLE_TASK_LOGS}"
        ).fetchone()["total"]
    assert remaining == 0


# ============================================================
# 八、删除
# ============================================================


def test_delete_task_removes_record(db: Path) -> None:
    """删除成功返回 True，且记录确实消失。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    assert sqlite_db.delete_task(created["id"], db_path=db) is True
    assert sqlite_db.get_task(created["id"], db_path=db) is None


def test_delete_task_missing_returns_false(db: Path) -> None:
    """删除不存在的任务返回 False。"""
    assert sqlite_db.delete_task(999, db_path=db) is False


def test_delete_task_keeps_others(db: Path) -> None:
    """删除只作用于目标任务。"""
    first, second = sqlite_db.batch_create(
        [sample_task(title="任务一"), sample_task(title="任务二")], db_path=db
    )
    sqlite_db.delete_task(first["id"], db_path=db)

    remaining = sqlite_db.list_tasks(db_path=db)
    assert len(remaining) == 1
    assert remaining[0]["id"] == second["id"]


# ============================================================
# 九、时间字段
# ============================================================


def test_timestamp_format_is_parsable(db: Path) -> None:
    """created_at / updated_at 均为可解析的时间字符串。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    assert datetime.fromisoformat(created["created_at"])
    assert datetime.fromisoformat(created["updated_at"])


def test_timestamps_not_decreasing(db: Path) -> None:
    """updated_at 不早于 created_at。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    assert created["updated_at"] >= created["created_at"]


def test_updated_at_refreshed_after_update(db: Path) -> None:
    """状态修改后 updated_at 被刷新且不早于原值。"""
    created = sqlite_db.create_task(sample_task(), db_path=db)
    sqlite_db.update_status(created["id"], "urgent", db_path=db)
    latest = sqlite_db.get_task(created["id"], db_path=db)

    assert latest["created_at"] == created["created_at"]
    assert latest["updated_at"] >= created["updated_at"]


def test_deadline_stored_as_iso_date(db: Path) -> None:
    """截止日期以 YYYY-MM-DD 形式存储。"""
    created = sqlite_db.create_task(sample_task(deadline="2026-09-30"), db_path=db)
    assert created["deadline"] == "2026-09-30"


def test_now_iso_is_second_precision() -> None:
    """统一的时间工具：秒级精度、格式稳定。"""
    stamp = sqlite_db.now_iso()
    assert datetime.fromisoformat(stamp)
    assert stamp.count(":") == 2
