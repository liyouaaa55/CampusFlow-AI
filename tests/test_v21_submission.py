"""v2.1 执行闭环单元测试：数据层、下发、回执、状态规则、汇总统计。

测试范围严格对应业务要求五组；全部使用 tmp_path 独立数据库，
不触碰真实的 data/campusflow.db，所有数据均为演示数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from database import models, sqlite_db, submission_store
from database.sqlite_db import TaskNotFoundError
from tools import dispatch_notice, submission_check, task_dispatch

TODAY = "2026-10-10"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "v21_test.db"
    sqlite_db.init_db(db_path)
    return db_path


def make_task(db: Path, **over) -> dict:
    task = {
        "title": "实验室安全检查",
        "department": "信息工程学院",
        "owner": "张三",
        "deadline": "2026-10-15",
        "priority": "high",
        "status": "pending",
        "risk_level": "green",
        "need_confirm": False,
        "description": "完成实验室安全检查并提交结果。",
    }
    task.update(over)
    return sqlite_db.create_task(task, db_path=db)


def table_columns(db_path: Path, table: str) -> list:
    with sqlite_db.get_connection(db_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in rows]


# ============================================================
# 一、数据库
# ============================================================


def test_submissions_table_created(db: Path) -> None:
    """回执表随连接自动建好（幂等）。"""
    with sqlite_db.get_connection(db) as connection:
        names = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        ]
    assert models.TABLE_TASK_SUBMISSIONS in names
    assert table_columns(db, models.TABLE_TASK_SUBMISSIONS) == list(
        models.SUBMISSION_ALL_COLUMNS
    )


def test_task_id_is_unique(db: Path) -> None:
    """一个任务只能有一条回执，由数据库 UNIQUE 约束保证。"""
    import sqlite3

    task = make_task(db)
    submission_store.save_submission(task_id=task["id"], content="第一次", db_path=db)
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite_db.get_connection(db) as connection:
            connection.execute(
                f"INSERT INTO {models.TABLE_TASK_SUBMISSIONS} "
                "(task_id, content, status, submitted_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task["id"], "重复插入", "submitted", "2026-10-09 10:00",
                 "2026-10-09 10:00", "2026-10-09 10:00"),
            )


def test_create_and_query_submission(db: Path) -> None:
    task = make_task(db)
    record = submission_store.save_submission(
        task_id=task["id"], content="已完成全部检查", submitted_at="2026-10-09 09:30", db_path=db
    )
    assert record["task_id"] == task["id"]
    assert record["content"] == "已完成全部检查"
    assert record["status"] == models.SUBMISSION_SUBMITTED

    found = submission_store.get_submission(task["id"], db_path=db)
    assert found is not None
    assert found["submitted_at"] == "2026-10-09 09:30"


def test_update_submission_keeps_single_row(db: Path) -> None:
    """重复提交更新原记录，不产生第二行。"""
    task = make_task(db)
    submission_store.save_submission(
        task_id=task["id"], content="初稿", submitted_at="2026-10-09 09:00", db_path=db
    )
    submission_store.save_submission(
        task_id=task["id"], content="补充后", submitted_at="2026-10-09 18:00", db_path=db
    )

    rows = submission_store.list_submissions(db_path=db)
    assert len(rows) == 1
    assert rows[0]["content"] == "补充后"
    assert rows[0]["submitted_at"] == "2026-10-09 18:00"


def test_submission_requires_content(db: Path) -> None:
    task = make_task(db)
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            submission_store.save_submission(task_id=task["id"], content=bad, db_path=db)


def test_submission_content_length_limited(db: Path) -> None:
    task = make_task(db)
    with pytest.raises(submission_store.ContentTooLongError):
        submission_store.save_submission(
            task_id=task["id"], content="长" * (submission_store.MAX_CONTENT_CHARS + 1), db_path=db
        )


def test_submission_rejects_missing_task(db: Path) -> None:
    with pytest.raises(TaskNotFoundError):
        submission_store.save_submission(task_id=9999, content="内容", db_path=db)


def test_tasks_isolated_between_tasks(db: Path) -> None:
    """任务隔离：A 的回执绝不出现在 B 上。"""
    first = make_task(db, title="任务甲")
    second = make_task(db, title="任务乙")

    submission_store.save_submission(task_id=first["id"], content="甲的完成情况", db_path=db)

    assert submission_store.get_submission(first["id"], db_path=db)["content"] == "甲的完成情况"
    assert submission_store.get_submission(second["id"], db_path=db) is None


def test_deleting_task_cascades_submission(db: Path) -> None:
    task = make_task(db)
    submission_store.save_submission(task_id=task["id"], content="内容", db_path=db)
    sqlite_db.delete_task(task["id"], db_path=db)
    assert submission_store.list_submissions(db_path=db) == []


def test_v20_tables_untouched(db: Path) -> None:
    """2.0 两张表的字段定义一字未改。"""
    assert table_columns(db, models.TABLE_TASKS) == list(models.TASK_ALL_COLUMNS)
    assert table_columns(db, models.TABLE_TASK_LOGS) == list(models.TASK_LOG_COLUMNS)


def test_v20_event_actions_unchanged() -> None:
    """执行链路动作不得混入 v2.0 事务事件契约。"""
    assert models.EVENT_ACTIONS == (
        "analyze", "draft", "request", "confirm", "reject", "escalate",
    )
    assert models.ACTION_DISPATCH not in models.EVENT_ACTIONS
    assert models.ACTION_SUBMIT not in models.EVENT_ACTIONS


# ============================================================
# 二、任务下发
# ============================================================


def test_normal_task_can_dispatch(db: Path) -> None:
    task = make_task(db)
    result = task_dispatch.dispatch_task(task["id"], db_path=db)
    assert result["ok"] is True
    assert result["already"] is False
    assert "系统内下发" in result["message"]


def test_dispatch_requires_owner(db: Path) -> None:
    """负责人为空禁止下发，且不得猜测。"""
    task = make_task(db, owner="")
    with pytest.raises(task_dispatch.OwnerRequiredError) as exc:
        task_dispatch.dispatch_task(task["id"], db_path=db)
    assert "负责人" in str(exc.value)
    assert submission_check.is_dispatched(task["id"], db_path=db) is False


def test_dispatch_notice_blocked_without_owner(db: Path) -> None:
    task = make_task(db, owner=None)
    with pytest.raises(task_dispatch.OwnerRequiredError):
        task_dispatch.prepare_notice(task, llm_fn=lambda s, u: {"notice": "x"})


def test_repeat_dispatch_is_idempotent(db: Path) -> None:
    """重复点击不产生第二条下发记录。"""
    task = make_task(db)
    task_dispatch.dispatch_task(task["id"], db_path=db)
    second = task_dispatch.dispatch_task(task["id"], db_path=db)

    assert second["already"] is True
    assert "已下发" in second["message"]
    actions = [x["action"] for x in sqlite_db.get_logs(task["id"], db_path=db)]
    assert actions.count(models.ACTION_DISPATCH) == 1


def test_dispatch_log_has_correct_task_id(db: Path) -> None:
    """下发日志必须绑定正确的 task_id，不串任务。"""
    first = make_task(db, title="任务甲")
    second = make_task(db, title="任务乙")
    task_dispatch.dispatch_task(first["id"], db_path=db)

    assert submission_check.is_dispatched(first["id"], db_path=db) is True
    assert submission_check.is_dispatched(second["id"], db_path=db) is False
    logs = sqlite_db.get_logs(first["id"], db_path=db)
    assert all(x["task_id"] == first["id"] for x in logs)


def test_dispatch_does_not_modify_tasks_table(db: Path) -> None:
    task = make_task(db)
    before = sqlite_db.get_task(task["id"], db_path=db)
    task_dispatch.dispatch_task(task["id"], db_path=db)
    assert sqlite_db.get_task(task["id"], db_path=db) == before


def test_dispatch_missing_task_raises(db: Path) -> None:
    with pytest.raises(TaskNotFoundError):
        task_dispatch.dispatch_task(9999, db_path=db)


def test_dispatch_never_mentions_external_platforms(db: Path) -> None:
    """系统内下发不得出现任何外部渠道字样。"""
    task = make_task(db)
    message = task_dispatch.dispatch_task(task["id"], db_path=db)["message"]
    for forbidden in ("微信", "企业微信", "短信", "邮件", "OA", "已发送"):
        assert forbidden not in message


def test_append_chain_log_rejects_unknown_action(db: Path) -> None:
    task = make_task(db)
    with pytest.raises(ValueError):
        sqlite_db.append_chain_log(task["id"], "随便什么", db_path=db)


# ============================================================
# 三、下发信息生成（LLM + 确定性回退）
# ============================================================


def test_notice_uses_llm_when_valid() -> None:
    task = {"title": "实验室安全检查", "owner": "张三", "department": "信息工程学院",
            "deadline": "2026-10-15"}
    result = dispatch_notice.generate_notice(
        task, llm_fn=lambda s, u: {"notice": "请张三于2026年10月15日前完成实验室安全检查。"}
    )
    assert result["source"] == "llm"
    assert "张三" in result["body"]


@pytest.mark.parametrize("bad", ["", "   ", None, 123, [], {"notice": ""}])
def test_notice_falls_back_on_invalid_llm_output(bad) -> None:
    task = {"title": "实验室安全检查", "owner": "张三", "deadline": "2026-10-15"}
    result = dispatch_notice.generate_notice(task, llm_fn=lambda s, u: bad)
    assert result["source"] == "template"
    assert result["degraded"] is True
    assert "张三" in result["body"]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("超时"), TimeoutError("timeout"), ValueError("429")],
)
def test_notice_falls_back_on_llm_exception(error) -> None:
    task = {"title": "实验室安全检查", "owner": "张三", "deadline": "2026-10-15"}

    def boom(system, user):
        raise error

    result = dispatch_notice.generate_notice(task, llm_fn=boom)
    assert result["source"] == "template"
    assert result["degraded"] is True


def test_notice_rejects_llm_rewriting_owner() -> None:
    """模型擅自换人 → 判定不合格并回退，绝不放行被改写的通知。"""
    task = {"title": "实验室安全检查", "owner": "张三", "deadline": "2026-10-15"}
    result = dispatch_notice.generate_notice(
        task, llm_fn=lambda s, u: {"notice": "请李四于2026年10月15日前完成实验室安全检查。"}
    )
    assert result["source"] == "template"
    assert "张三" in result["body"]


def test_notice_rejects_llm_rewriting_title() -> None:
    task = {"title": "实验室安全检查", "owner": "张三"}
    result = dispatch_notice.generate_notice(
        task, llm_fn=lambda s, u: {"notice": "请张三完成消防安全检查。"}
    )
    assert result["source"] == "template"


def test_notice_never_fabricates_owner_or_deadline() -> None:
    """缺失字段必须写占位词，绝不臆造。"""
    task = {"title": "仅有标题"}
    result = dispatch_notice.generate_notice(task, llm_fn=lambda s, u: {"notice": "请尽快完成。"})
    body = result["body"]
    assert dispatch_notice.UNKNOWN_OWNER in body
    assert dispatch_notice.UNKNOWN_DEADLINE in body


def test_template_keeps_title_and_owner_verbatim() -> None:
    task = {"title": "实验室安全检查", "owner": "张三", "deadline": "2026-10-15"}
    result = dispatch_notice.render_template(task)
    assert "实验室安全检查" in result["body"]
    assert "张三" in result["body"]
    assert "2026 年 10 月 15 日" in result["body"]


def test_notice_generation_never_raises(db: Path) -> None:
    """无论发生什么，都必须给出一份可展示的通知。"""
    task = {"title": "T", "owner": "O", "deadline": "2026-10-15"}

    def boom(system, user):
        raise RuntimeError("boom")

    assert dispatch_notice.generate_notice(task, llm_fn=boom)["body"]


def test_notice_does_not_modify_task(db: Path) -> None:
    """生成通知是只读操作，绝不回写任务。"""
    task = make_task(db)
    before = sqlite_db.get_task(task["id"], db_path=db)
    task_dispatch.prepare_notice(task, llm_fn=lambda s, u: {"notice": "请张三完成任务。"})
    assert sqlite_db.get_task(task["id"], db_path=db) == before


# ============================================================
# 四、工作检查窗口
# ============================================================


def test_check_view_loads_correct_task(db: Path) -> None:
    task = make_task(db)
    view = task_dispatch.task_check_view(task["id"], today=TODAY, db_path=db)
    assert view["task"]["id"] == task["id"]
    assert view["task"]["title"] == "实验室安全检查"
    assert view["state"] == submission_check.STATE_PENDING


def test_check_view_missing_task(db: Path) -> None:
    with pytest.raises(TaskNotFoundError):
        task_dispatch.task_check_view(9999, db_path=db)


def test_switching_task_does_not_leak_previous_content(db: Path) -> None:
    """防串任务：切到 B 之后绝不能看到 A 的回执内容。"""
    first = make_task(db, title="任务甲")
    second = make_task(db, title="任务乙")
    task_dispatch.submit_work(first["id"], "甲的完成情况", db_path=db)

    view_b = task_dispatch.task_check_view(second["id"], today=TODAY, db_path=db)
    assert view_b["submission"] is None
    assert view_b["state"] == submission_check.STATE_PENDING
    assert view_b["task"]["title"] == "任务乙"

    view_a = task_dispatch.task_check_view(first["id"], today=TODAY, db_path=db)
    assert view_a["submission"]["content"] == "甲的完成情况"


def test_submit_work_succeeds(db: Path) -> None:
    task = make_task(db)
    result = task_dispatch.submit_work(
        task["id"], "已完成全部检查", submitted_at="2026-10-09 08:00", db_path=db
    )
    assert result["ok"] is True
    assert result["updated"] is False
    assert result["submission"]["content"] == "已完成全部检查"


def test_submit_work_requires_content(db: Path) -> None:
    task = make_task(db)
    for bad in ("", "   ", None):
        with pytest.raises(task_dispatch.ContentRequiredError):
            task_dispatch.submit_work(task["id"], bad, db_path=db)


def test_submit_work_missing_task(db: Path) -> None:
    with pytest.raises(TaskNotFoundError):
        task_dispatch.submit_work(9999, "内容", db_path=db)


def test_repeat_submit_updates_instead_of_duplicating(db: Path) -> None:
    """重复提交不产生第二条 Submission。"""
    task = make_task(db)
    task_dispatch.submit_work(task["id"], "初稿", submitted_at="2026-10-09 08:00", db_path=db)
    result = task_dispatch.submit_work(
        task["id"], "补充材料已附", submitted_at="2026-10-09 20:00", db_path=db
    )

    assert result["updated"] is True
    rows = submission_store.list_submissions(db_path=db)
    assert len(rows) == 1
    assert rows[0]["content"] == "补充材料已附"
    # 首次提交才写 submit 日志，补填不重复刷事件链
    actions = [x["action"] for x in sqlite_db.get_logs(task["id"], db_path=db)]
    assert actions.count(models.ACTION_SUBMIT) == 1


def test_submit_with_attachment(db: Path) -> None:
    task = make_task(db)
    result = task_dispatch.submit_work(
        task["id"], "已完成", file_path="检查记录表.xlsx", db_path=db
    )
    assert result["submission"]["file_path"] == "检查记录表.xlsx"


def test_attachment_path_traversal_is_stripped(db: Path) -> None:
    """附件路径只取文件名，杜绝路径穿越。"""
    task = make_task(db)
    result = task_dispatch.submit_work(
        task["id"], "已完成", file_path="../../../etc/passwd", db_path=db
    )
    assert result["submission"]["file_path"] == "passwd"


def test_submit_without_attachment(db: Path) -> None:
    """附件非必填。"""
    task = make_task(db)
    result = task_dispatch.submit_work(task["id"], "已完成", db_path=db)
    assert result["submission"]["file_path"] is None


def test_full_event_chain(db: Path) -> None:
    """完整事件链：create → dispatch → submit。"""
    task = make_task(db)
    task_dispatch.dispatch_task(task["id"], db_path=db)
    task_dispatch.submit_work(task["id"], "已完成", db_path=db)
    assert [x["action"] for x in sqlite_db.get_logs(task["id"], db_path=db)] == [
        models.ACTION_CREATE,
        models.ACTION_DISPATCH,
        models.ACTION_SUBMIT,
    ]


# --- 回执状态规则 ---


def test_state_pending_before_deadline(db: Path) -> None:
    task = make_task(db)
    view = task_dispatch.task_check_view(task["id"], today="2026-10-10", db_path=db)
    assert view["state"] == submission_check.STATE_PENDING


def test_state_submitted_when_record_exists(db: Path) -> None:
    task = make_task(db)
    task_dispatch.submit_work(task["id"], "已完成", db_path=db)
    view = task_dispatch.task_check_view(task["id"], today="2026-10-20", db_path=db)
    assert view["state"] == submission_check.STATE_SUBMITTED


def test_state_overdue_unsubmitted(db: Path) -> None:
    """超过截止时间且未提交 → 已逾期未提交。"""
    task = make_task(db, deadline="2026-10-01")
    view = task_dispatch.task_check_view(task["id"], today="2026-10-10", db_path=db)
    assert view["state"] == submission_check.STATE_OVERDUE


def test_state_not_overdue_on_deadline_day(db: Path) -> None:
    """截止日当天不算逾期。"""
    task = make_task(db, deadline="2026-10-10")
    view = task_dispatch.task_check_view(task["id"], today="2026-10-10", db_path=db)
    assert view["state"] == submission_check.STATE_PENDING


def test_state_without_deadline_never_overdue(db: Path) -> None:
    """无截止时间不判定逾期，绝不臆造期限。"""
    task = make_task(db, deadline="")
    view = task_dispatch.task_check_view(task["id"], today="2027-01-01", db_path=db)
    assert view["state"] == submission_check.STATE_PENDING


def test_submission_state_does_not_use_llm() -> None:
    """状态判定必须是纯函数，不依赖任何模型。"""
    import inspect

    source = inspect.getsource(submission_check)
    assert "llm" not in source.lower()
    assert "client" not in source.lower()


# ============================================================
# 五、执行检查报告
# ============================================================


def test_summary_counts_are_correct(db: Path) -> None:
    """构造 4 个任务：已提交 / 待提交 / 已逾期未提交 / 未下发。"""
    submitted = make_task(db, title="A", deadline="2026-10-20")
    pending = make_task(db, title="B", deadline="2026-10-20")
    overdue = make_task(db, title="C", deadline="2026-10-01")
    not_dispatched = make_task(db, title="D", deadline="2026-10-20")

    for task in (submitted, pending, overdue):
        task_dispatch.dispatch_task(task["id"], db_path=db)
    task_dispatch.submit_work(submitted["id"], "已完成", db_path=db)

    data = task_dispatch.summary(today="2026-10-10", db_path=db)

    assert data["total"] == 4
    assert data["dispatched"] == 3
    assert data["submitted"] == 1
    assert data["pending"] == 2
    assert data["overdue"] == 1
    assert data["rate"] == 25.0


def test_summary_rate_rounding(db: Path) -> None:
    for index in range(3):
        task = make_task(db, title=f"T{index}", deadline="2026-10-20")
        task_dispatch.dispatch_task(task["id"], db_path=db)
        if index < 1:
            task_dispatch.submit_work(task["id"], "已完成", db_path=db)

    data = task_dispatch.summary(today="2026-10-10", db_path=db)
    assert data["submitted"] == 1
    assert data["rate"] == 33.3


def test_summary_empty_database(db: Path) -> None:
    """空库不除零，提交率为 0.0。"""
    data = task_dispatch.summary(today=TODAY, db_path=db)
    assert data["total"] == 0
    assert data["rate"] == 0.0
    assert data["rows"] == []


def test_summary_detail_keeps_real_owner(db: Path) -> None:
    """明细里的负责人必须来自任务表，不串数据。"""
    first = make_task(db, title="A", owner="张三", deadline="2026-10-20")
    second = make_task(db, title="B", owner="李四", deadline="2026-10-20")
    task_dispatch.submit_work(first["id"], "已完成", db_path=db)

    rows = {row["task_id"]: row for row in task_dispatch.summary(today=TODAY, db_path=db)["rows"]}
    assert rows[first["id"]]["owner"] == "张三"
    assert rows[first["id"]]["state"] == submission_check.STATE_SUBMITTED
    assert rows[second["id"]]["owner"] == "李四"
    assert rows[second["id"]]["state"] == submission_check.STATE_PENDING


def test_summary_is_isolated_between_tasks(db: Path) -> None:
    """A 已提交绝不影响 B 的统计。"""
    first = make_task(db, title="A")
    second = make_task(db, title="B")
    task_dispatch.submit_work(first["id"], "已完成", db_path=db)

    rows = {row["task_id"]: row for row in task_dispatch.summary(today=TODAY, db_path=db)["rows"]}
    assert rows[first["id"]]["state"] == "submitted"
    assert rows[second["id"]]["state"] == "pending_submit"


# ============================================================
# 六、中文标签（不泄漏英文枚举）
# ============================================================


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("pending_submit", "待提交"),
        ("submitted", "已提交"),
        ("overdue_unsubmitted", "已逾期未提交"),
    ],
)
def test_submission_labels_are_chinese(state: str, expected: str) -> None:
    from ui import labels

    assert labels.submission_label(state) == expected


def test_submission_label_never_leaks_english() -> None:
    from ui import labels

    for state in labels.SUBMISSION_ORDER:
        rendered = labels.submission_label(state)
        assert rendered != state
        assert any("一" <= ch <= "鿿" for ch in rendered)


def test_submission_label_unknown_falls_back() -> None:
    from ui import labels

    assert labels.submission_label("never_defined") == labels.UNKNOWN_TEXT
    assert labels.submission_label(None) == labels.UNKNOWN_TEXT


def test_chain_actions_have_chinese_labels() -> None:
    """新增的 dispatch / submit 必须有中文标签，否则日志表显示占位符。"""
    from ui import labels

    assert labels.log_action_label(models.ACTION_DISPATCH) == "系统内下发"
    assert labels.log_action_label(models.ACTION_SUBMIT) == "提交工作回执"


def test_submission_labels_disjoint_from_task_statuses() -> None:
    """两套状态必须独立，绝不混用。"""
    from ui import labels

    assert set(labels.SUBMISSION_LABELS).isdisjoint(set(models.TASK_STATUSES))
    assert set(labels.SUBMISSION_ORDER) == set(labels.SUBMISSION_LABELS)
