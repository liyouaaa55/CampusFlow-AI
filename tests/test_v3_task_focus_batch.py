"""v3.0 第四阶段：任务来源定位与第三页展示排序（真实行为测试）。

覆盖目标（全部为**运行行为断言**，不使用源码字符串断言）：
    1. 入库后「本次任务」来自 batch_create() 真实返回的 ID，并被 focus 定位；
    2. 多任务入库保留整批 ID，且第三页默认选中本次任务中的一条；
    3. 本批为空 / focus 无效时不误选历史任务，也不误报成功；
    4. 第三页列表按「最新创建优先」展示，数据库默认排序契约不变；
    5. 切换筛选后不保留指向旧任务的陈旧选择；
    6. 选择器、详情、下发、日志使用同一 task_id；
    7. 第四页与第五页渲染不受影响。

安全约定：全部使用 tmp_path 隔离数据库（monkeypatch sqlite_db.DB_PATH），
并把 llm.client.chat_json 替换为抛错桩，绝不调用真实大模型、绝不触碰生产库。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import pytest

from config import settings
from database import sqlite_db
from tools import task_dispatch
from ui.components import state, tables
from ui.pages import nav, review_page
from ui.pages import tasks_page as tp

pytest.importorskip("streamlit")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = str(settings.BASE_DIR / "app.py")


# ============================================================
# 夹具与工具
# ============================================================


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离数据库：应用、业务服务与默认连接全部指向它。"""
    path = tmp_path / "task_focus_batch.db"
    sqlite_db.init_db(path)
    monkeypatch.setattr(sqlite_db, "DB_PATH", path)

    from llm import client

    def _no_llm(*args: object, **kwargs: object) -> object:
        raise client.LLMError("本测试不调用真实大模型")

    monkeypatch.setattr(client, "chat_json", _no_llm)
    return path


_SESSION_KEYS = (
    state.KEY_LAST_IMPORT_IDS,
    state.KEY_LAST_IMPORT_AT,
    state.KEY_FOCUS_TASK,
    tp.KEY_TASK_SELECT,
    tp.KEY_TASK_VIEW,
)


@pytest.fixture(autouse=True)
def _reset_session_state() -> Any:
    """每个用例前后清掉与本次定位相关的会话键，避免相互污染。"""
    for key in _SESSION_KEYS:
        st.session_state.pop(key, None)
    yield
    for key in _SESSION_KEYS:
        st.session_state.pop(key, None)


def _task(title: str, *, status: str = "pending", owner: str = "王老师") -> dict[str, Any]:
    return {
        "title": title,
        "department": "学工办",
        "owner": owner,
        "deadline": "2099-12-31",
        "priority": "medium",
        "status": status,
        "risk_level": "green",
        "description": f"{title}的说明",
    }


def _seed(
    db: Path,
    titles: Sequence[str],
    *,
    status: str = "pending",
    owners: Sequence[str] | None = None,
) -> list[int]:
    """批量入库并返回真实任务 ID（与 batch_create 返回值同序）。"""
    payload = [
        _task(title, status=status, owner=(owners[index] if owners else "王老师"))
        for index, title in enumerate(titles)
    ]
    created = sqlite_db.batch_create(payload, db_path=db)
    return [int(record["id"]) for record in created]


def _widget_by_key(collection: Any, key: str) -> Any:
    return next((item for item in collection if getattr(item, "key", None) == key), None)


def _open_tasks_page(app: "AppTest") -> None:
    radio = next((item for item in app.radio if nav.PAGE_TASKS in list(item.options)), None)
    assert radio is not None, "未找到侧边栏导航"
    radio.set_value(nav.PAGE_TASKS)
    app.run()


def _open_app() -> "AppTest":
    app = AppTest.from_file(APP_PATH, default_timeout=120)
    app.run()
    return app


def _markdown_text(app: "AppTest") -> str:
    return "\n".join(str(item.value) for item in app.markdown)


def _caption_text(app: "AppTest") -> str:
    return "\n".join(str(item.value) for item in app.caption)


def _title_text(app: "AppTest") -> str:
    return "\n".join(str(item.value) for item in app.title)


# ============================================================
# 一、入库：本次任务 ID 的真实来源与 focus 传递
# ============================================================


def test_created_ids_returns_real_ids_from_batch_create(db: Path) -> None:
    """_created_ids() 取到的必须是数据库真实存在的 ID，且与返回记录同序。"""
    _seed(db, ["历史任务"])
    created = sqlite_db.batch_create([_task("新材料甲"), _task("新材料乙")], db_path=db)
    ids = review_page._created_ids(created)

    assert ids == [int(record["id"]) for record in created]
    assert len(ids) == 2
    real = {int(task["id"]): task["title"] for task in sqlite_db.list_tasks(db_path=db)}
    assert [real[i] for i in ids] == ["新材料甲", "新材料乙"]


def test_created_ids_skips_dirty_records() -> None:
    """缺 id / 非法 id / 重复 id 都要被安全跳过，不抛异常。"""
    assert review_page._created_ids(
        [{"id": 7}, {"id": None}, {}, {"id": "8"}, {"id": 7}, {"ID": 9}]
    ) == [7, 8]


def test_import_tasks_sets_focus_and_batch_to_real_ids(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实跑一遍入库：focus 与「本次任务」都指向刚创建的任务，而不是历史任务。"""
    monkeypatch.setattr(review_page.st, "rerun", lambda *a, **k: None)
    historical = _seed(db, ["历史任务一", "历史任务二"])

    frame = pd.DataFrame(
        [
            {
                review_page.COL_TITLE: "新材料任务甲",
                review_page.COL_OWNER: "李老师",
                review_page.COL_DEPARTMENT: "后勤处",
                review_page.COL_DEADLINE: date(2099, 12, 31),
                review_page.COL_PRIORITY: "中",
                review_page.COL_DESCRIPTION: "新材料说明甲",
            },
            {
                review_page.COL_TITLE: "新材料任务乙",
                review_page.COL_OWNER: "赵老师",
                review_page.COL_DEPARTMENT: "教务处",
                review_page.COL_DEADLINE: date(2099, 12, 31),
                review_page.COL_PRIORITY: "高",
                review_page.COL_DESCRIPTION: "新材料说明乙",
            },
        ]
    )
    review_page._import_tasks(frame)

    batch = state.get_last_import_ids()
    assert len(batch) == 2
    titles = {int(t["id"]): t["title"] for t in sqlite_db.list_tasks(db_path=db)}
    assert [titles[i] for i in batch] == ["新材料任务甲", "新材料任务乙"]
    assert set(batch).isdisjoint(set(historical)), "本次任务不得指向历史任务"
    assert state.get_last_import_at(), "应记录本次入库时间"
    assert state.pop_focus_task() == batch[0], "应定位到本次入库的第一个任务"
    assert state.get_last_import_ids() == batch, "focus 被消费后本批集合仍应保留"


def test_import_tasks_does_not_claim_success_without_ids(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """batch_create 未返回有效记录时：报错、不动定位、不误报成功。"""
    errors: list[str] = []
    monkeypatch.setattr(review_page.st, "error", errors.append)
    monkeypatch.setattr(review_page.st, "rerun", lambda *a, **k: None)
    monkeypatch.setattr(review_page.sqlite_db, "batch_create", lambda *a, **k: [])

    _seed(db, ["历史任务"])

    frame = pd.DataFrame(
        [
            {
                review_page.COL_TITLE: "可能失败的任务",
                review_page.COL_OWNER: "李老师",
                review_page.COL_DEPARTMENT: "后勤处",
                review_page.COL_DEADLINE: date(2099, 12, 31),
                review_page.COL_PRIORITY: "中",
                review_page.COL_DESCRIPTION: "说明",
            }
        ]
    )
    review_page._import_tasks(frame)

    assert errors, "未返回有效记录时必须给出错误提示"
    assert state.get_last_import_ids() == [], "不得写入空的本次任务集合"
    assert state.pop_focus_task() is None, "不得定位到任何任务"


def test_last_import_state_cleaning() -> None:
    """会话状态读写：去重、跳过脏数据、空集合不覆盖已有记录。"""
    state.set_last_import([5, "6", 5, None, 7], created_at="2026-10-10 12:00:00")
    assert state.get_last_import_ids() == [5, 6, 7]
    assert state.get_last_import_at() == "2026-10-10 12:00:00"

    state.set_last_import([], created_at="")
    assert state.get_last_import_ids() == [5, 6, 7], "空集合不得覆盖已有记录"

    state.clear_last_import()
    assert state.get_last_import_ids() == []
    assert state.get_last_import_at() == ""


# ============================================================
# 二、默认选中规则与最新优先排序
# ============================================================


def _options_of(tasks: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    options = {tables.task_option_label(task): task for task in tasks}
    return options, list(options)


def test_default_label_prefers_focus_then_batch_then_newest() -> None:
    """默认选中优先级：focus > 本次入库任务 > 列表第一条（最新）。"""
    newest_first = [
        {"id": 30, "title": "最新任务", "department": "学工办", "owner": "王老师"},
        {"id": 20, "title": "中间任务", "department": "学工办", "owner": "王老师"},
        {"id": 10, "title": "最旧任务", "department": "学工办", "owner": "王老师"},
    ]
    options, option_list = _options_of(newest_first)

    label, reason = tp._pick_default_label(options, option_list, 10, [20])
    assert reason == tp.DEFAULT_FOCUS and options[label]["id"] == 10

    label, reason = tp._pick_default_label(options, option_list, None, [20, 10])
    assert reason == tp.DEFAULT_BATCH and options[label]["id"] == 20

    label, reason = tp._pick_default_label(options, option_list, None, [])
    assert reason == tp.DEFAULT_NEWEST and options[label]["id"] == 30


def test_default_label_falls_back_to_newest_for_invalid_ids() -> None:
    """无效 focus / 无效本批 ID 时退回最新任务，绝不指向不存在或陈旧对象。"""
    newest_first = [
        {"id": 5, "title": "最新任务", "department": "学工办", "owner": "王老师"},
        {"id": 1, "title": "历史任务", "department": "学工办", "owner": "王老师"},
    ]
    options, option_list = _options_of(newest_first)

    label, reason = tp._pick_default_label(options, option_list, 999, [888])
    assert reason == tp.DEFAULT_NEWEST
    assert options[label]["id"] == 5, "无有效 ID 时选最新任务"

    assert tp._pick_default_label({}, [], None, []) == ("", tp.DEFAULT_NEWEST)
    assert tp._label_for_id(options, option_list, None) is None
    assert tp._label_for_id(options, option_list, "abc") is None


def test_list_tasks_newest_first_keeps_default_contract(db: Path) -> None:
    """页面可要最新优先；数据库默认仍是入库顺序（既有契约不变）。"""
    ids = _seed(db, ["甲", "乙", "丙"])
    assert [t["id"] for t in sqlite_db.list_tasks(db_path=db)] == ids
    assert [t["id"] for t in sqlite_db.list_tasks(db_path=db, newest_first=True)] == list(
        reversed(ids)
    )


def test_apply_filters_requests_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """第三页取数必须显式要求「最新优先」，筛选条件仍需下推。"""
    captured: dict[str, Any] = {}

    def fake_list_tasks(
        status: str | None = None,
        department: str | None = None,
        db_path: Any = None,
        *,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        captured.update(status=status, department=department, newest_first=newest_first)
        return [{"id": 1}]

    monkeypatch.setattr(tp.sqlite_db, "list_tasks", fake_list_tasks)
    result = tp._apply_filters(
        {"status": "processing", "department": "学工办", "owner": "", "priority": None, "risk": None}
    )

    assert captured == {"status": "processing", "department": "学工办", "newest_first": True}
    assert result == [{"id": 1}]


# ============================================================
# 三、本次任务视图：只认真实 ID，不退回历史任务
# ============================================================


def test_filter_batch_tasks_keeps_only_real_batch_ids(db: Path) -> None:
    """「本次生成的任务」只保留本批真实 ID 对应的任务。"""
    ids = _seed(db, ["历史甲", "本次乙", "本次丙"])
    all_tasks = sqlite_db.list_tasks(db_path=db, newest_first=True)

    wanted = set(ids[1:])
    kept = tp._filter_batch_tasks(all_tasks, all_tasks, ids[1:])
    assert {task["id"] for task in kept} == wanted
    assert [task["id"] for task in kept] == [
        task["id"] for task in all_tasks if task["id"] in wanted
    ], "应保持入参（最新优先）顺序，只做筛选"
    assert tp._filter_batch_tasks(all_tasks, all_tasks, []) == []


def test_filter_batch_tasks_never_falls_back_to_history(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本批任务不存在或全被筛选排除时：返回空 + 明确提示，不拿历史任务顶替。"""
    all_tasks = _seed_rows(db)

    warnings: list[str] = []
    infos: list[str] = []
    monkeypatch.setattr(tp.st, "warning", warnings.append)
    monkeypatch.setattr(tp.st, "info", infos.append)

    missing = tp._filter_batch_tasks(all_tasks, all_tasks, [999999])
    assert missing == []
    assert warnings and "已不存在" in warnings[0]

    infos.clear()
    filtered_out = tp._filter_batch_tasks([], all_tasks, [all_tasks[0]["id"]])
    assert filtered_out == []
    assert infos and "筛选条件排除" in infos[0]


def _seed_rows(db: Path) -> list[dict[str, Any]]:
    _seed(db, ["历史甲", "历史乙"])
    return sqlite_db.list_tasks(db_path=db, newest_first=True)


# ============================================================
# 四、真实页面（AppTest）：默认选中、批次视图、筛选变化、页面保护
# ============================================================


def test_tasks_page_defaults_to_newest_task(db: Path) -> None:
    """无 focus、无本次记录时，第三页默认选中最新任务（而非最旧的历史任务）。"""
    ids = _seed(db, ["最旧任务", "中间任务", "最新任务"])
    app = _open_app()
    _open_tasks_page(app)

    assert not app.exception
    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    assert select is not None, "任务选择器应存在且带稳定 key"
    newest_label = tables.task_option_label(sqlite_db.get_task(ids[-1], db_path=db))
    assert select.options[0] == newest_label
    assert select.value == newest_label
    assert _markdown_text(app).count("全部任务") >= 1


def test_tasks_page_batch_view_and_focus(db: Path) -> None:
    """注入「本次入库」与 focus 后：默认批次视图、只显示本批任务、选中本批任务。"""
    _seed(db, ["历史甲", "历史乙", "历史丙", "历史丁"])
    imported = _seed(db, ["本次甲", "本次乙"])
    app = _open_app()

    app.session_state[state.KEY_LAST_IMPORT_IDS] = imported
    app.session_state[state.KEY_LAST_IMPORT_AT] = "2026-10-10 12:00:00"
    app.session_state[state.KEY_FOCUS_TASK] = imported[0]
    app.run()
    _open_tasks_page(app)

    assert not app.exception
    view = _widget_by_key(app.radio, tp.KEY_TASK_VIEW)
    assert view is not None
    assert list(view.options) == [tp.VIEW_BATCH, tp.VIEW_ALL]
    assert view.value == tp.VIEW_BATCH

    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    assert select is not None
    assert list(select.options) == [
        tables.task_option_label(sqlite_db.get_task(task_id, db_path=db))
        for task_id in reversed(imported)
    ], "批次视图下只应出现本批两项任务（按最新优先展示）"
    assert select.value == tables.task_option_label(
        sqlite_db.get_task(imported[0], db_path=db)
    ), "应先定位到本次入库任务"

    text = _markdown_text(app)
    assert "本次生成的任务（2 / 6 条）" in text
    caption_text = _caption_text(app)
    assert "仅存活于当前会话" in caption_text, "必须诚实标注本次任务仅当前会话可见"
    assert "已确认入库 2 项任务" in caption_text

    # 切回「全部任务」：历史任务恢复可见
    view = _widget_by_key(app.radio, tp.KEY_TASK_VIEW)
    view.set_value(tp.VIEW_ALL)
    app.run()
    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    assert len(select.options) == 6
    assert "全部任务（6 / 6 条）" in _markdown_text(app)


def test_tasks_page_resets_stale_selection_when_filter_changes(db: Path) -> None:
    """切换筛选导致原选中项不在选项里时，必须重置到有效任务，不保留陈旧选择。"""
    pending_id = _seed(db, ["待办任务"])[0]
    done_id = _seed(db, ["已完成任务"], status="completed")[0]

    app = _open_app()
    _open_tasks_page(app)

    pending_label = tables.task_option_label(sqlite_db.get_task(pending_id, db_path=db))
    done_label = tables.task_option_label(sqlite_db.get_task(done_id, db_path=db))

    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    select.set_value(pending_label)
    app.run()

    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    assert select.value == pending_label

    status_label = next(
        label for label, value in tables.status_options().items() if value == "completed"
    )
    status_filter = next(item for item in app.selectbox if item.label == "状态")
    status_filter.set_value(status_label)
    app.run()

    assert not app.exception
    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    assert list(select.options) == [done_label]
    assert select.value == done_label, "陈旧选中项必须被安全重置"


def test_tasks_page_selection_drives_workspace_task_id(db: Path) -> None:
    """选择器选中的任务，其详情区必须展示同一 task_id 的数据（不串号）。"""
    ids = _seed(db, ["任务甲", "任务乙"], owners=["王老师", "李老师"])
    app = _open_app()
    _open_tasks_page(app)

    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    oldest_label = select.options[-1]  # 最旧的一条 = 任务甲（王老师）
    select.set_value(oldest_label)
    app.run()

    assert not app.exception
    select = _widget_by_key(app.selectbox, tp.KEY_TASK_SELECT)
    assert select.value == oldest_label
    assert select.value != select.options[0]

    target = sqlite_db.get_task(ids[0], db_path=db)
    other = sqlite_db.get_task(ids[1], db_path=db)
    detail_text = _markdown_text(app)
    assert "任务详情" in detail_text, "应渲染被选中任务的详情卡片"
    assert target["owner"] in detail_text
    assert other["owner"] not in detail_text, "详情区不得混入其他任务的数据"


def test_fourth_and_fifth_pages_still_render(db: Path) -> None:
    """保护性回归：第四页（反馈/核验/闭环）与第五页（报告）仍正常渲染。"""
    ids = _seed(db, ["任务甲", "任务乙"])
    task_dispatch.dispatch_task(ids[0], db_path=db)
    app = _open_app()

    _open_tasks_page(app)
    assert not app.exception

    inspection = next(
        (item for item in app.radio if nav.PAGE_INSPECTION in list(item.options)), None
    )
    inspection.set_value(nav.PAGE_INSPECTION)
    app.run()
    assert not app.exception
    assert "执行反馈与检查" in _title_text(app)
    assert any(item.label == "查看范围" for item in app.radio)
    assert any(item.label == "反馈状态" for item in app.radio), "第四页执行反馈入口应存在"

    report = next((item for item in app.radio if nav.PAGE_REPORT in list(item.options)), None)
    report.set_value(nav.PAGE_REPORT)
    app.run()
    assert not app.exception
    assert "工作检查报告" in _title_text(app)
    report_text = _markdown_text(app)
    assert "# 工作检查报告" in report_text, "第五页报告正文仍应生成"
    assert "任务明细表" in report_text
    assert any(button.label == "重新生成" for button in app.button)
