"""ReminderAgent 单元测试。

测试策略：
- 级别判定等纯函数直接断言；
- 扫描类接口使用 tmp_path 下的独立数据库（真实走 SQLite Memory，不 mock）；
- 基准日期固定为 2026-09-23（TODAY），保证临期 / 紧急 / 逾期判定稳定。
所有任务数据均为演示数据（Synthetic Data）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent import reminder_agent
from config import settings
from database import sqlite_db
from tools import report_tool

# 固定基准日期
TODAY = date(2026, 9, 23)

# 混合场景数据：(标题, 部门, 相对今天的天数, 状态)
MIXED_SEED: list[tuple[str, str, int | None, str]] = [
    ("巡检校园监控设备", "保卫处", 10, "pending"),          # normal
    ("组织新生入学教育", "学工办", 5, "pending"),           # normal
    ("更新实验室台账", "实验中心", 4, "confirmed"),         # normal
    ("举办机器人创新周", "学工办", 3, "processing"),        # due_soon
    ("消防器材年检", "保卫处", 2, "processing"),            # due_soon
    ("报送教学质量报告", "教务处", 1, "processing"),        # urgent
    ("完成安全隐患排查", "教务处", 0, "processing"),        # urgent（当天截止）
    ("宿舍消防隐患专项排查", "学工办", -1, "pending"),      # overdue
    ("整理活动总结材料", "学工办", -3, "processing"),       # overdue
]

# 期望的提醒排序（逾期 → 紧急 → 临期 → 正常；同级按剩余天数升序）
EXPECTED_ORDER = [
    "整理活动总结材料",
    "宿舍消防隐患专项排查",
    "完成安全隐患排查",
    "报送教学质量报告",
    "消防器材年检",
    "举办机器人创新周",
    "更新实验室台账",
    "组织新生入学教育",
    "巡检校园监控设备",
]


def deadline_for(offset: int | None) -> str | None:
    """把相对天数转换为 YYYY-MM-DD；None 表示无截止时间。"""
    return None if offset is None else (TODAY + timedelta(days=offset)).isoformat()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """独立的临时数据库。"""
    db_path = tmp_path / "reminder_test.db"
    sqlite_db.init_db(db_path)
    return db_path


def add_task(
    db_path: Path,
    *,
    title: str = "举办机器人创新周",
    department: str = "学工办",
    owner: str | None = "李老师",
    offset: int | None = 5,
    status: str = "pending",
    priority: str = "medium",
) -> dict:
    """向数据库写入一条任务，返回入库记录。"""
    return sqlite_db.create_task(
        {
            "title": title,
            "department": department,
            "owner": owner,
            "deadline": deadline_for(offset),
            "priority": priority,
            "status": status,
            "risk_level": "green",
            "need_confirm": False,
            "description": None,
        },
        db_path=db_path,
    )


@pytest.fixture()
def mixed_db(db: Path) -> Path:
    """预置 9 条覆盖四个级别的任务。"""
    sqlite_db.batch_create(
        [
            {
                "title": title,
                "department": department,
                "owner": "李老师",
                "deadline": deadline_for(offset),
                "priority": "medium",
                "status": status,
                "risk_level": "green",
                "need_confirm": False,
                "description": None,
            }
            for title, department, offset, status in MIXED_SEED
        ],
        db_path=db,
    )
    return db


# ============================================================
# 一、级别判定（纯函数，阈值来自 settings）
# ============================================================


@pytest.mark.parametrize("days", [-1, -3, -30])
def test_compute_level_overdue(days: int) -> None:
    """剩余天数小于 0 → overdue。"""
    assert reminder_agent.compute_level(days) == "overdue"


@pytest.mark.parametrize("days", [0, 1])
def test_compute_level_urgent(days: int) -> None:
    """剩余 0~1 天 → urgent（天数 0 即当天截止）。"""
    assert reminder_agent.compute_level(days) == "urgent"


@pytest.mark.parametrize("days", [2, 3])
def test_compute_level_due_soon(days: int) -> None:
    """剩余 2~3 天 → due_soon。"""
    assert reminder_agent.compute_level(days) == "due_soon"


@pytest.mark.parametrize("days", [4, 5, 10, 100])
def test_compute_level_normal(days: int) -> None:
    """剩余 4 天及以上 → normal。"""
    assert reminder_agent.compute_level(days) == "normal"


def test_thresholds_come_from_settings() -> None:
    """阈值必须来自 settings，而不是写死在代码里。"""
    assert reminder_agent.compute_level(settings.REMINDER_URGENT_DAYS) == "urgent"
    assert reminder_agent.compute_level(settings.REMINDER_URGENT_DAYS + 1) == "due_soon"
    assert reminder_agent.compute_level(settings.REMINDER_DUE_SOON_DAYS) == "due_soon"
    assert reminder_agent.compute_level(settings.REMINDER_DUE_SOON_DAYS + 1) == "normal"


@pytest.mark.parametrize(("offset", "expected"), [(0, 0), (1, 1), (5, 5), (-2, -2)])
def test_compute_days_left(offset: int, expected: int) -> None:
    """剩余天数 = 截止日 - 基准日。"""
    assert reminder_agent.compute_days_left(deadline_for(offset), TODAY) == expected


@pytest.mark.parametrize("deadline", [None, "", "   "])
def test_compute_days_left_missing(deadline: object) -> None:
    """截止时间缺失时返回 None。"""
    assert reminder_agent.compute_days_left(deadline, TODAY) is None


@pytest.mark.parametrize("deadline", ["下周三", "2026/09/30", "2026-13-01", "待定"])
def test_compute_days_left_invalid(deadline: str) -> None:
    """截止时间无法解析时返回 None（不做日期推断）。"""
    assert reminder_agent.compute_days_left(deadline, TODAY) is None


# ============================================================
# 二、提醒文案
# ============================================================


def test_message_for_urgent_with_one_day() -> None:
    """需求示例：剩余 1 天的文案。"""
    assert reminder_agent.build_message("urgent", 1) == "距离截止还有1天，请及时完成。"


def test_message_for_due_today() -> None:
    """当天截止使用专门文案，避免「还有 0 天」。"""
    assert reminder_agent.build_message("urgent", 0) == reminder_agent.MESSAGE_TODAY


def test_message_for_overdue_uses_absolute_days() -> None:
    """逾期文案使用天数绝对值。"""
    assert reminder_agent.build_message("overdue", -3) == "已逾期3天，请立即处理。"


@pytest.mark.parametrize("level", ["due_soon", "normal"])
def test_message_contains_days(level: str) -> None:
    """临期 / 正常文案包含剩余天数。"""
    assert "2天" in reminder_agent.build_message(level, 2)


# ============================================================
# 三、check_all：全量扫描
# ============================================================


def test_check_all_empty_database(db: Path) -> None:
    """空数据库返回空列表。"""
    assert reminder_agent.check_all(today=TODAY, db_path=db) == []


def test_check_all_normal_task(db: Path) -> None:
    """正常任务被识别为 normal 并带上剩余天数。"""
    add_task(db, offset=10)
    items = reminder_agent.check_all(today=TODAY, db_path=db)

    assert len(items) == 1
    assert items[0]["level"] == "normal"
    assert items[0]["days_left"] == 10


def test_reminder_fields_and_order(db: Path) -> None:
    """提醒对象字段与顺序必须与需求文档一致。"""
    add_task(db, offset=1)
    item = reminder_agent.check_all(today=TODAY, db_path=db)[0]
    assert list(item.keys()) == list(reminder_agent.REMINDER_FIELDS)


def test_task_id_and_identity_fields(db: Path) -> None:
    """提醒对象携带任务标识、负责人与部门，便于推送。"""
    created = add_task(db, title="宿舍消防隐患专项排查", department="保卫处", owner="王老师", offset=-1)
    item = reminder_agent.check_all(today=TODAY, db_path=db)[0]

    assert item["task_id"] == created["id"]
    assert item["title"] == "宿舍消防隐患专项排查"
    assert item["department"] == "保卫处"
    assert item["owner"] == "王老师"


def test_days_left_is_int(db: Path) -> None:
    """days_left 必须为整数，供前端直接展示。"""
    add_task(db, offset=-2)
    assert isinstance(reminder_agent.check_all(today=TODAY, db_path=db)[0]["days_left"], int)


def test_all_four_levels_detected(mixed_db: Path) -> None:
    """混合数据能识别出全部四个级别。"""
    levels = {item["level"] for item in reminder_agent.check_all(today=TODAY, db_path=mixed_db)}
    assert levels == set(reminder_agent.REMINDER_LEVELS)


def test_reminder_sorting(mixed_db: Path) -> None:
    """排序：逾期 → 紧急 → 临期 → 正常，同级按剩余天数升序。"""
    titles = [item["title"] for item in reminder_agent.check_all(today=TODAY, db_path=mixed_db)]
    assert titles == EXPECTED_ORDER


def test_today_is_injectable(db: Path) -> None:
    """基准日期可注入：同一任务在不同基准日下级别会变化。"""
    add_task(db, offset=10)
    assert reminder_agent.check_all(today=TODAY, db_path=db)[0]["level"] == "normal"
    later = TODAY + timedelta(days=8)
    assert reminder_agent.check_all(today=later, db_path=db)[0]["level"] == "due_soon"


def test_completed_tasks_are_skipped(db: Path) -> None:
    """已完成任务默认不再督办。"""
    add_task(db, title="已完成的活动", offset=-5, status="completed")
    add_task(db, title="进行中的活动", offset=5, status="processing")

    titles = [item["title"] for item in reminder_agent.check_all(today=TODAY, db_path=db)]
    assert titles == ["进行中的活动"]


def test_explicit_status_filter_includes_completed(db: Path) -> None:
    """显式按状态筛选时以调用方筛选为准（即使筛选已完成）。"""
    add_task(db, title="已完成的活动", offset=-5, status="completed")
    items = reminder_agent.check_all(status="completed", today=TODAY, db_path=db)

    assert len(items) == 1
    assert items[0]["level"] == "overdue"


def test_status_filter(db: Path) -> None:
    """支持 list_tasks(status="processing") 式的输入。"""
    add_task(db, title="进行中的活动", offset=3, status="processing")
    add_task(db, title="待办的活动", offset=3, status="pending")

    titles = [item["title"] for item in reminder_agent.check_all(status="processing", today=TODAY, db_path=db)]
    assert titles == ["进行中的活动"]


def test_task_without_deadline_is_skipped(db: Path) -> None:
    """缺少截止时间的任务无法判级，不作为提醒对象。"""
    add_task(db, title="无截止时间的任务", offset=None)
    assert reminder_agent.check_all(today=TODAY, db_path=db) == []


def test_injected_task_with_invalid_deadline_is_skipped(tmp_path: Path) -> None:
    """注入数据中日期非法的任务同样被跳过。"""
    tasks = [{"id": 1, "title": "日期非法", "deadline": "下周三", "status": "pending"}]
    assert reminder_agent.check_all(tasks=tasks, today=TODAY, db_path=tmp_path / "unused.db") == []


def test_injected_tasks_do_not_touch_database(tmp_path: Path) -> None:
    """注入 tasks 时不访问数据库（文件不会被创建）。"""
    db_file = tmp_path / "never_created.db"
    tasks = [{"id": 7, "title": "注入任务", "deadline": deadline_for(1), "status": "pending",
              "department": "学工办", "owner": "李老师"}]

    items = reminder_agent.check_all(tasks=tasks, today=TODAY, db_path=db_file)
    assert items[0]["task_id"] == 7
    assert not db_file.exists()


def test_injected_tasks_support_filters() -> None:
    """注入的任务同样支持状态 / 部门筛选。"""
    tasks = [
        {"id": 1, "title": "A", "deadline": deadline_for(1), "status": "processing", "department": "学工办"},
        {"id": 2, "title": "B", "deadline": deadline_for(1), "status": "pending", "department": "学工办"},
    ]
    assert [item["title"] for item in reminder_agent.check_all(status="processing", tasks=tasks, today=TODAY)] == ["A"]
    assert reminder_agent.check_department("教务处", tasks=tasks, today=TODAY) == []


def test_check_all_rejects_non_list_tasks() -> None:
    """tasks 参数类型错误时报错。"""
    with pytest.raises(TypeError):
        reminder_agent.check_all(tasks={"id": 1}, today=TODAY)  # type: ignore[arg-type]


def test_databases_are_isolated(tmp_path: Path) -> None:
    """不同数据库互不干扰。"""
    db_a = sqlite_db.init_db(tmp_path / "a.db")
    db_b = sqlite_db.init_db(tmp_path / "b.db")
    add_task(db_a, title="A库任务", offset=1)

    assert len(reminder_agent.check_all(today=TODAY, db_path=db_a)) == 1
    assert reminder_agent.check_all(today=TODAY, db_path=db_b) == []


# ============================================================
# 四、check_department：跨部门
# ============================================================


def test_check_department_only_returns_that_department(mixed_db: Path) -> None:
    """只返回指定部门的提醒。"""
    items = reminder_agent.check_department("教务处", today=TODAY, db_path=mixed_db)
    assert [item["title"] for item in items] == ["完成安全隐患排查", "报送教学质量报告"]
    assert {item["department"] for item in items} == {"教务处"}


def test_check_department_with_status_filter(mixed_db: Path) -> None:
    """部门 + 状态组合筛选（结果同样按紧急程度排序）。"""
    items = reminder_agent.check_department("学工办", status="processing", today=TODAY, db_path=mixed_db)
    assert [item["title"] for item in items] == ["整理活动总结材料", "举办机器人创新周"]


def test_check_department_unknown_returns_empty(mixed_db: Path) -> None:
    """不存在的部门返回空列表。"""
    assert reminder_agent.check_department("不存在的部门", today=TODAY, db_path=mixed_db) == []


def test_departments_are_independent(mixed_db: Path) -> None:
    """跨部门统计互不串扰。"""
    xuegong = reminder_agent.count_levels(department="学工办", today=TODAY, db_path=mixed_db)
    baowei = reminder_agent.count_levels(department="保卫处", today=TODAY, db_path=mixed_db)

    assert sum(xuegong.values()) == 4
    assert sum(baowei.values()) == 2
    # 学工办：+5 正常、+3 临期、-1 与 -3 逾期
    assert xuegong == {"normal": 1, "due_soon": 1, "urgent": 0, "overdue": 2}
    # 保卫处：+10 正常、+2 临期
    assert baowei == {"normal": 1, "due_soon": 1, "urgent": 0, "overdue": 0}


# ============================================================
# 五、check_task：单任务督办
# ============================================================


def test_check_task_returns_reminder(db: Path) -> None:
    """单任务检查返回提醒对象。"""
    created = add_task(db, title="消防器材年检", offset=2)
    item = reminder_agent.check_task(created["id"], today=TODAY, db_path=db)

    assert item is not None
    assert item["level"] == "due_soon"
    assert list(item.keys()) == list(reminder_agent.REMINDER_FIELDS)


def test_check_task_missing_returns_none(db: Path) -> None:
    """任务不存在返回 None。"""
    assert reminder_agent.check_task(999, today=TODAY, db_path=db) is None


def test_check_task_completed_returns_none(db: Path) -> None:
    """已完成任务不再督办。"""
    created = add_task(db, offset=-1, status="completed")
    assert reminder_agent.check_task(created["id"], today=TODAY, db_path=db) is None


def test_check_task_without_deadline_returns_none(db: Path) -> None:
    """无截止时间的任务返回 None。"""
    created = add_task(db, offset=None)
    assert reminder_agent.check_task(created["id"], today=TODAY, db_path=db) is None


def test_check_task_consistent_with_check_all(db: Path) -> None:
    """单任务结果与全量扫描中的同一条一致（除排序外字段相同）。"""
    created = add_task(db, title="宿舍消防隐患专项排查", offset=-1)
    single = reminder_agent.check_task(created["id"], today=TODAY, db_path=db)
    listed = [item for item in reminder_agent.check_all(today=TODAY, db_path=db) if item["task_id"] == created["id"]]

    assert listed == [single]


# ============================================================
# 六、count_levels：统计（供 Dashboard）
# ============================================================


def test_count_levels_keys_and_order(db: Path) -> None:
    """返回固定的四个键，顺序为 normal / due_soon / urgent / overdue。"""
    counts = reminder_agent.count_levels(today=TODAY, db_path=db)
    assert list(counts.keys()) == list(reminder_agent.REMINDER_LEVELS)


def test_count_levels_empty_database(db: Path) -> None:
    """空数据库各计数均为 0。"""
    assert reminder_agent.count_levels(today=TODAY, db_path=db) == {
        "normal": 0,
        "due_soon": 0,
        "urgent": 0,
        "overdue": 0,
    }


def test_count_levels_mixed_data(mixed_db: Path) -> None:
    """混合数据统计正确（9 条：3 正常 / 2 临期 / 2 紧急 / 2 逾期）。"""
    assert reminder_agent.count_levels(today=TODAY, db_path=mixed_db) == {
        "normal": 3,
        "due_soon": 2,
        "urgent": 2,
        "overdue": 2,
    }


def test_count_levels_matches_check_all(mixed_db: Path) -> None:
    """统计结果与扫描结果完全一致。"""
    items = reminder_agent.check_all(today=TODAY, db_path=mixed_db)
    counts = reminder_agent.count_levels(today=TODAY, db_path=mixed_db)

    assert sum(counts.values()) == len(items)


def test_count_levels_values_are_ints(mixed_db: Path) -> None:
    """计数值均为整数。"""
    assert all(isinstance(value, int) for value in reminder_agent.count_levels(today=TODAY, db_path=mixed_db).values())


def test_count_levels_with_status_filter(mixed_db: Path) -> None:
    """支持按状态统计。"""
    counts = reminder_agent.count_levels(status="processing", today=TODAY, db_path=mixed_db)
    assert sum(counts.values()) == 5


def test_count_levels_with_items_injection(mixed_db: Path) -> None:
    """可直接传入手工构造的提醒列表进行统计。"""
    items = [{"level": "urgent"}, {"level": "urgent"}, {"level": "unknown"}]
    assert reminder_agent.count_levels(items=items) == {
        "normal": 0,
        "due_soon": 0,
        "urgent": 2,
        "overdue": 0,
    }


def test_count_levels_ignores_completed(mixed_db: Path) -> None:
    """已完成任务不计入统计。"""
    sqlite_db.create_task(
        {
            "title": "已完成任务",
            "deadline": deadline_for(-10),
            "status": "completed",
            "priority": "medium",
        },
        db_path=mixed_db,
    )
    assert reminder_agent.count_levels(today=TODAY, db_path=mixed_db)["overdue"] == 2


# ============================================================
# 七、generate_messages：提醒文案
# ============================================================


def test_generate_messages_length(mixed_db: Path) -> None:
    """文案数量与提醒条数一致。"""
    assert len(reminder_agent.generate_messages(today=TODAY, db_path=mixed_db)) == 9


def test_generate_messages_content(mixed_db: Path) -> None:
    """文案包含级别中文标签、任务标题与负责人。"""
    message = reminder_agent.generate_messages(today=TODAY, db_path=mixed_db)[0]
    assert message.startswith("【已逾期】")
    assert "整理活动总结材料" in message
    assert "李老师" in message


def test_generate_messages_empty_database(db: Path) -> None:
    """无任务时返回空列表。"""
    assert reminder_agent.generate_messages(today=TODAY, db_path=db) == []


def test_generate_messages_overdue_wording(db: Path) -> None:
    """逾期文案包含逾期天数与「立即处理」。"""
    add_task(db, title="逾期任务", offset=-2)
    message = reminder_agent.generate_messages(today=TODAY, db_path=db)[0]
    assert "已逾期2天" in message
    assert "立即处理" in message


def test_format_reminder_message_handles_missing_fields() -> None:
    """负责人 / 部门缺失时展示为「未指定」，不出现 None。"""
    message = reminder_agent.format_reminder_message(
        {"title": "无负责人的任务", "level": "urgent", "days_left": 1, "owner": None,
         "department": None, "message": "距离截止还有1天，请及时完成。"}
    )
    assert "None" not in message
    assert "未指定" in message


def test_format_reminder_message_accepts_items(mixed_db: Path) -> None:
    """可直接把已有提醒列表转换为文案。"""
    items = reminder_agent.check_all(today=TODAY, db_path=mixed_db)
    messages = reminder_agent.generate_messages(items=items)
    assert len(messages) == len(items)


# ============================================================
# 八、等级定义文件（knowledge/rules/reminder.json）
# ============================================================


def test_load_level_rules_from_knowledge_file() -> None:
    """真实规则文件包含四个级别的 label / color。"""
    rules = reminder_agent.load_level_rules()
    assert set(reminder_agent.REMINDER_LEVELS).issubset(set(rules))
    assert rules["urgent"]["label"] == "临期"
    assert rules["overdue"]["color"] == "red"
    assert all(rules[level]["label"] for level in reminder_agent.REMINDER_LEVELS)


def test_level_label_and_color() -> None:
    """展示用的中文名与颜色来自规则文件。"""
    assert reminder_agent.level_label("due_soon") == "即将到期"
    assert reminder_agent.level_color("urgent") == "orange"


def test_level_label_unknown_level_fallback() -> None:
    """未知级别原样返回，不抛异常。"""
    assert reminder_agent.level_label("unknown") == "unknown"


def test_load_level_rules_missing_file_raises(tmp_path: Path) -> None:
    """规则文件缺失时报错。"""
    with pytest.raises(reminder_agent.ReminderRulesError):
        reminder_agent.load_level_rules(tmp_path / "not_exists.json")


@pytest.mark.parametrize(
    "content",
    [
        "{不是合法 JSON",
        '{"levels": {}}',
        '["normal"]',
        '{"levels": {"normal": "正常"}}',
    ],
)
def test_load_level_rules_invalid_file_raises(tmp_path: Path, content: str) -> None:
    """格式非法时抛错。"""
    rules_file = tmp_path / "reminder.json"
    rules_file.write_text(content, encoding="utf-8")
    with pytest.raises(reminder_agent.ReminderRulesError):
        reminder_agent.load_level_rules(rules_file)


def test_load_level_rules_requires_all_levels(tmp_path: Path) -> None:
    """缺少任一必须级别时报错，避免出现无法展示的等级。"""
    rules_file = tmp_path / "reminder.json"
    rules_file.write_text(
        json.dumps({"levels": {"due_soon": {"label": "临期"}, "urgent": {"label": "紧急"},
                               "overdue": {"label": "已逾期"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(reminder_agent.ReminderRulesError):
        reminder_agent.load_level_rules(rules_file)


def test_load_level_rules_cache_and_reload(tmp_path: Path) -> None:
    """按路径缓存，force_reload 可强制重读。"""
    rules_file = tmp_path / "reminder.json"

    def write(label: str) -> None:
        rules_file.write_text(
            json.dumps(
                {
                    "levels": {
                        "normal": {"label": label},
                        "due_soon": {"label": "临期"},
                        "urgent": {"label": "紧急"},
                        "overdue": {"label": "已逾期"},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write("正常")
    assert reminder_agent.load_level_rules(rules_file)["normal"]["label"] == "正常"

    write("一般")
    assert reminder_agent.load_level_rules(rules_file)["normal"]["label"] == "正常"      # 命中缓存
    assert reminder_agent.load_level_rules(rules_file, force_reload=True)["normal"]["label"] == "一般"


def test_rules_path_lives_in_knowledge_rules() -> None:
    """等级定义文件位于 knowledge/rules 下。"""
    assert reminder_agent.REMINDER_RULES_PATH == settings.RULES_DIR / "reminder.json"
    assert reminder_agent.REMINDER_RULES_PATH.exists()


# ============================================================
# 九、渲染工具（tools/report_tool.py）
# ============================================================


def test_render_level_counts_markdown(mixed_db: Path) -> None:
    """统计结果渲染为 Markdown 表格并含合计行。"""
    counts = reminder_agent.count_levels(today=TODAY, db_path=mixed_db)
    text = report_tool.render_level_counts(counts)

    assert "| 级别 | 数量 |" in text
    assert "| 正常 | 3 |" in text
    assert "| 已逾期 | 2 |" in text
    assert "| 合计 | 9 |" in text


def test_render_level_counts_custom_labels() -> None:
    """可传入自定义级别名称。"""
    text = report_tool.render_level_counts({"urgent": 1}, labels={"urgent": "非常紧急"})
    assert "非常紧急" in text


def test_render_reminders_markdown(mixed_db: Path) -> None:
    """提醒列表渲染为 Markdown 列表，含标签与天数描述。"""
    items = reminder_agent.check_all(today=TODAY, db_path=mixed_db)
    text = report_tool.render_reminders(items)

    assert "- **整理活动总结材料**（已逾期，逾期 3 天）" in text
    assert "- **完成安全隐患排查**（临期，剩余 0 天）" in text
    assert text.count("- **") == len(items)


def test_render_reminders_empty() -> None:
    """空列表返回友好提示。"""
    assert report_tool.render_reminders([]) == report_tool.EMPTY_REMINDER_TEXT
