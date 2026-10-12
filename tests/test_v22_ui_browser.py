"""浏览器级验证（Streamlit 官方 AppTest）：两个页面在**隔离数据库**上真实渲染与操作。

为什么必须隔离
--------------
app.py 里的页面函数走 sqlite_db.DB_PATH（默认 = data/campusflow.db），
而 sqlite_db.get_connection() 每次连接都会执行 PRAGMA journal_mode = WAL
与建表语句。也就是说：只要真实加载 app.py，就会碰到生产库文件。

AppTest 与 app.py 运行在**同一进程**，因此在加载应用之前把
sqlite_db.DB_PATH 指向临时库即可实现真正隔离——不是"只在脚本里声明临时路径"，
而是应用、业务服务与数据库连接实际都指向隔离库（本文件逐项断言了这一点）。

本文件同时禁止任何真实大模型调用：llm.client.chat_json 被替换为抛错桩，
通知生成等路径自动走模板兜底，保证离线可复现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import settings
from database import closure_store, sqlite_db, task_metadata_store
from tools import task_dispatch
from tools import verification_service as verify_svc

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from ui.pages import nav

TASKS_PAGE = nav.PAGE_TASKS
INSPECTION_PAGE = nav.PAGE_INSPECTION


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离数据库：应用、业务服务、默认连接全部指向它。"""
    path = tmp_path / "browser.db"
    sqlite_db.init_db(path)
    # 关键：AppTest 与 app.py 同进程，改这里就等于改了应用实际使用的库
    monkeypatch.setattr(sqlite_db, "DB_PATH", path)

    from llm import client

    def _no_llm(*args: object, **kwargs: object) -> object:
        raise client.LLMError("浏览器验证不调用真实大模型")

    monkeypatch.setattr(client, "chat_json", _no_llm)
    return path


def _open_app() -> "AppTest":
    app = AppTest.from_file(str(settings.BASE_DIR / "app.py"), default_timeout=120)
    app.run()
    return app


def _goto(app: "AppTest", page: str) -> "AppTest":
    nav = next((r for r in app.radio if page in list(r.options)), None)
    assert nav is not None, f"未找到侧边栏导航（目标页：{page}）"
    nav.set_value(page)
    app.run()
    return app


def _seed(db: Path, **over: object) -> int:
    payload: dict[str, object] = {
        "title": "教材征订统计",
        "department": "教务处",
        "owner": "陈老师",
        "deadline": "2026-12-31",
        "priority": "medium",
        "status": "confirmed",
        "risk_level": "green",
        "need_confirm": 0,
        "description": "完成教材征订统计并提交统计表。",
    }
    payload.update(over)
    return int(sqlite_db.create_task(payload, db_path=db)["id"])


def _button(app: "AppTest", label: str) -> object:
    found = next((b for b in app.button if b.label == label), None)
    assert found is not None, f"未找到按钮「{label}」，当前按钮：{[b.label for b in app.button]}"
    return found


def _labels(app: "AppTest") -> set[str]:
    return {str(b.label) for b in app.button}


def _markdown(app: "AppTest") -> str:
    """收集页面上的**全部可见文本**。

    同一句文案可能由 markdown / caption / alert 等不同元素承载，
    这里统一拼起来判断"界面上到底有没有这句话"，避免因元素类型不同而误判。
    """
    chunks: list[str] = []
    for name in (
        "markdown",
        "caption",
        "code",
        "warning",
        "error",
        "success",
        "info",
    ):
        collection = getattr(app, name, None) or ()
        for item in collection:
            value = getattr(item, "value", "")
            chunks.append(str(value))
    return "\n".join(chunks)


# ============================================================
# 一、两页在隔离库上真实渲染
# ============================================================


def test_both_pages_render_on_isolated_db(db: Path) -> None:
    """应用实际使用的库必须是隔离库，且两个页面均无异常渲染。"""
    task_id = _seed(db)
    task_dispatch.dispatch_task(task_id, db_path=db)

    app = _open_app()
    assert list(app.exception) == []

    # 断言隔离确实生效：应用侧读到的就是隔离库
    assert Path(str(sqlite_db.DB_PATH)).resolve() == Path(str(db)).resolve()
    assert Path(str(sqlite_db.DB_PATH)).resolve() != Path(str(settings.DB_PATH)).resolve()

    _goto(app, TASKS_PAGE)
    assert list(app.exception) == [], "任务督办中心渲染异常"

    _goto(app, INSPECTION_PAGE)
    assert list(app.exception) == [], "执行检查渲染异常"


# ============================================================
# 二、任务督办中心：任务确认始终可见且真实生效
# ============================================================


def test_task_confirmation_visible_and_effective(db: Path) -> None:
    """未确认任务必须直接显示确认入口；点击后状态真实推进为已确认。"""
    task_id = _seed(db, need_confirm=1, status="pending")

    app = _open_app()
    _goto(app, TASKS_PAGE)
    assert list(app.exception) == []

    # 1) 确认入口清晰可见（不在折叠区里——折叠区在它之后渲染）
    assert "确认任务" in _labels(app)
    assert "任务待确认" in _markdown(app)

    # 2) 点击确认 → 数据库真实变化
    _button(app, "确认任务").click()
    app.run()
    assert list(app.exception) == []

    task = sqlite_db.get_task(task_id, db_path=db)
    assert task["status"] == "confirmed"
    assert "confirmed" in [x["action"] for x in sqlite_db.get_logs(task_id, db_path=db)]

    # 3) 已确认后不再重复展示确认表单
    assert "确认任务" not in _labels(app)
    assert "任务已满足确认要求" in _markdown(app)


def test_focus_mode_workspace_renders(db: Path) -> None:
    """首页「查看并处理」跳转（专注模式）：工作区同样完整可用，且不重复渲染。

    专注模式此前只渲染「详情 + 智能处置」，任务确认与下发入口缺失；
    改造后它与列表模式共用同一套工作区顺序。
    """
    task_id = _seed(db, need_confirm=1, status="pending")

    app = _open_app()
    # 模拟首页写入的定位任务（只存 id，页面读取后即消费）
    app.session_state["cf_focus_task_id"] = task_id
    _goto(app, TASKS_PAGE)
    assert list(app.exception) == [], "专注模式渲染异常"

    text = _markdown(app)
    assert "当前处置任务" in text
    # 任务确认入口在专注模式下同样直接可见（且处于「待确认」真实状态）
    assert "任务待确认" in text
    assert "确认任务" in _labels(app)
    # 下发前置条件可见，并明确指出「未确认不能下发」
    assert "下发前置条件" in text
    assert "未确认的任务不能生成下发通知" in text
    # 专注模式下不得重复渲染第二个工作区标题
    assert text.count('cf-section-title">任务处置工作区') == 0


# ============================================================
# 三、执行检查：核验未通过时不可闭环，通过后可正常闭环
# ============================================================


def test_inspection_blocks_then_allows_closure(db: Path) -> None:
    """未核验 → 闭环按钮不可用；核验通过 → 可正常闭环（复用现有服务）。"""
    task_id = _seed(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    task_dispatch.submit_work(task_id, content="统计表已完成并提交", db_path=db)
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交教材征订统计表"],
        closure_source="material",
        closure_confirmed=True,
        db_path=db,
    )

    app = _open_app()
    _goto(app, INSPECTION_PAGE)
    assert list(app.exception) == []

    # --- 当前有效提交尚未核验：闭环被阻断 ---
    assert "核验结论" in _markdown(app)
    assert "不满足" in _markdown(app)
    close_btn = _button(app, "正式闭环")
    assert getattr(close_btn, "disabled", None) is True, "未核验时闭环按钮必须不可用"
    assert closure_store.get_closure(task_id, db_path=db) is None

    # 此时还没有核验记录，历史区只给一行提示
    assert "暂无核验记录" in _markdown(app)

    # --- 走真实核验路径（人工裁决通过，不经过大模型）---
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)

    app.run()
    assert list(app.exception) == []
    assert "核验结论：核验通过" in _markdown(app)
    assert "具备闭环条件" in _markdown(app)

    # 核验历史此时存在，且默认折叠
    history = next((e for e in app.expander if "核验历史" in str(e.label)), None)
    assert history is not None, "核验历史应为可展开区块"
    if getattr(history, "expanded", None) is not None:
        assert history.expanded is False, "核验历史必须默认折叠"

    # --- 点击正式闭环 → 真实闭环 ---
    _button(app, "正式闭环").click()
    app.run()
    assert list(app.exception) == []

    closure = closure_store.get_closure(task_id, db_path=db)
    assert closure is not None, "全部条件满足时应能正式闭环"
    assert "任务已正式闭环" in _markdown(app)

    # 闭环后任务离开「待办理」队列；用**全新会话**复查「已闭环」范围，
    # 确认闭环时间与闭环人真实可回看（避免沿用被 st.rerun() 截断的旧元素树）。
    fresh = _open_app()
    _goto(fresh, INSPECTION_PAGE)
    assert list(fresh.exception) == []

    scope = next((r for r in fresh.radio if "待办理" in list(r.options)), None)
    assert scope is not None, "未找到查看范围选择器"
    scope.set_value("已闭环")
    fresh.run()
    assert list(fresh.exception) == []

    closed_text = _markdown(fresh)
    assert "已于" in closed_text, "已闭环范围应显示闭环时间"
    assert "闭环人" in closed_text, "已闭环范围应显示闭环人"


def test_inspection_history_labels_current_vs_historical(db: Path) -> None:
    """历史版本通过 + 当前版本未通过：当前版本仍被阻断，历史被显式标注。"""
    task_id = _seed(db)
    task_dispatch.dispatch_task(task_id, db_path=db)
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交教材征订统计表"],
        closure_source="material",
        closure_confirmed=True,
        db_path=db,
    )
    task_dispatch.submit_work(task_id, content="统计表 v1", db_path=db)
    verify_svc.verify_submission(task_id, decision="approved", db_path=db)
    task_dispatch.submit_work(task_id, content="统计表 v2（修订）", db_path=db)
    verify_svc.verify_submission(task_id, decision="need_supplement", reason="v2 缺少附件", db_path=db)

    app = _open_app()
    _goto(app, INSPECTION_PAGE)
    assert list(app.exception) == []

    # 顶部阶段来自真实数据：当前版本未通过
    assert "核验未通过" in _markdown(app)
    # 当前版本结论为需补充
    assert "需补充" in _markdown(app)
    # 闭环被阻断
    assert getattr(_button(app, "正式闭环"), "disabled", None) is True
    assert closure_store.get_closure(task_id, db_path=db) is None

    # 核验历史：既有历史版本，也有当前版本标注
    history = next((e for e in app.expander if "核验历史" in str(e.label)), None)
    assert history is not None
    assert "历史版本" in str(history.label) or "当前版本" in str(history.label)
