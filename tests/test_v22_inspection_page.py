"""执行检查页面闭环链路测试（v2.2 最小可交付）。

覆盖「提交 → 核验 → 闭环」在页面业务层的真实行为。
全部使用真实 service（verification_service / closure_service），
仅把 LLM 换成假模型，不直接写库构造核验结论。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config import settings
from database import (
    closure_store,
    models,
    sqlite_db,
    submission_store,
    task_metadata_store,
    verification_store,
)
from tools import closure_service as closure_svc
from tools import task_dispatch
from ui import labels
from ui.pages import inspection_page as ip

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "inspection.db"
    sqlite_db.init_db(p)
    return p


def _fake_llm(payload: Any) -> Any:
    def _call(system_prompt: str, user_prompt: str) -> Any:
        return payload
    return _call


# 注意：verification_service._ask_ai 要求 AI 返回带 confidence 的对象，
# 置信度低于阈值会转人工复核（这是「AI 不得自行判定通过」的设计）。
APPROVED_PAYLOAD: dict[str, Any] = {
    "confidence": 0.95,
    "requirement_match": True,
    "reason": "材料齐备，符合办结要求",
}

REJECTED_PAYLOAD: dict[str, Any] = {
    "confidence": 0.30,
    "requirement_match": False,
    "reason": "缺少整改照片",
}


def _approve(task_id: int, db_path: Path, operator: str = "李主任") -> dict[str, Any]:
    """以督办人员人工裁决取得 approved 结论。

    服务的设计是「AI 不得自行判定通过」：高风险、或事实检查未全通过时，
    自动判定只会落到 manual_review / need_supplement。approved 必须来自
    督办人员的显式裁决，这里走 decision 参数这条真实路径。
    """
    outcome = ip.run_verification(
        task_id, operator=operator,
        decision=models.VERIFICATION_APPROVED,
        reason="人工复核后确认通过", db_path=db_path,
    )
    assert outcome["ok"] is True
    assert outcome["result"]["status"] == models.VERIFICATION_APPROVED
    return outcome["result"]


def _seed(db_path: Path, *, risk_level: str = "green", criteria: bool = True,
          content: str = "整改验收完成，现场照片已留存") -> int:
    """建一条可闭环的任务：下发 → 提交 → 办结标准。"""
    task_id = int(sqlite_db.create_task(
        {
            "title": "实验室安全检查",
            "department": "保卫处",
            "owner": "王老师",
            "deadline": "2026-11-01",
            "status": "processing",
            "risk_level": risk_level,
        },
        db_path=db_path,
    )["id"])
    task_dispatch.dispatch_task(task_id, operator="AI", db_path=db_path)
    # 走真实提交入口：它同时写入 submit 链路日志，
    # 否则 _check_process 会判定「未按流程提交」，自动核验不可能通过。
    task_dispatch.submit_work(task_id, content=content, db_path=db_path)
    if criteria:
        task_metadata_store.set_closure_criteria(
            task_id, ["整改验收完成"], closure_source="material",
            closure_confirmed=True, db_path=db_path,
        )
    return task_id


def test_inspection_page_registered_in_navigation() -> None:
    """第四页必须已注册进侧边栏导航（v3.0 第二阶段正式名称：执行反馈与检查）。"""
    from app import PAGES, PAGE_RENDERERS
    from ui.pages import nav

    assert nav.PAGE_INSPECTION in PAGES
    assert PAGE_RENDERERS[nav.PAGE_INSPECTION] is ip.render
    assert PAGES.index(nav.PAGE_INSPECTION) < PAGES.index(nav.PAGE_REPORT)


def test_inspection_page_renders_without_exception() -> None:
    """页面真实渲染不得抛出任何异常。"""
    from ui.pages import nav as nav_module

    app = AppTest.from_file(str(settings.BASE_DIR / "app.py"), default_timeout=60)
    app.run()
    nav = next((r for r in app.radio if nav_module.PAGE_INSPECTION in list(r.options)), None)
    assert nav is not None, "未找到导航控件"
    nav.set_value(nav_module.PAGE_INSPECTION)
    app.run()
    assert list(app.exception) == [], f"页面异常：{list(app.exception)}"


def test_submission_can_enter_verification(db: Path) -> None:
    """已提交任务能进入核验流程并拿到 approved 结论。"""
    task_id = _seed(db)
    outcome = ip.run_verification(
        task_id, operator="李主任", llm_fn=_fake_llm(APPROVED_PAYLOAD), db_path=db
    )
    assert outcome["ok"] is True
    result = outcome["result"]
    assert result["ok"] is True
    # 服务设计：AI 置信度足够 + 事实检查通过 才自动 approved
    assert result["status"] == models.VERIFICATION_APPROVED
    assert verification_store.latest_verification(task_id, db_path=db) is not None

def test_full_chain_submit_verify_close(db: Path) -> None:
    """完整链路：提交 → 核验通过 → 正式闭环。"""
    task_id = _seed(db)
    _approve(task_id, db)

    c = ip.run_closure(task_id, closed_by="李主任", closure_note="验收通过", db_path=db)
    assert c["ok"] is True
    assert c["result"]["ok"] is True
    assert closure_store.is_closed(task_id, db_path=db) is True
    assert models.ACTION_CLOSE in [
        x["action"] for x in sqlite_db.get_logs(task_id, db_path=db)
    ]


def test_verification_failed_cannot_close(db: Path) -> None:
    """核验不通过 → 闭环被拒绝。"""
    task_id = _seed(db)
    ip.run_verification(
        task_id, operator="李主任", llm_fn=_fake_llm(REJECTED_PAYLOAD), db_path=db
    )
    c = ip.run_closure(task_id, closed_by="李主任", db_path=db)
    assert c["result"]["ok"] is False
    assert closure_store.is_closed(task_id, db_path=db) is False


def test_unverified_cannot_close(db: Path) -> None:
    """已提交但未核验 → 闭环被拒绝。"""
    task_id = _seed(db)
    c = ip.run_closure(task_id, closed_by="李主任", db_path=db)
    assert c["result"]["ok"] is False
    assert "核验" in str(c["result"]["reason"])


def test_criteria_unconfirmed_cannot_close(db: Path) -> None:
    """办结标准未确认 → 闭环被拒绝。"""
    task_id = _seed(db, criteria=False)
    _approve(task_id, db)
    c = ip.run_closure(task_id, closed_by="李主任", db_path=db)
    assert c["result"]["ok"] is False
    assert "办结标准" in str(c["result"]["reason"])


def test_high_risk_requires_ack(db: Path) -> None:
    """高风险任务缺少授权理由 → 拒绝；补理由后放行。"""
    task_id = _seed(db, risk_level="red")
    _approve(task_id, db)
    denied = ip.run_closure(task_id, closed_by="李主任", db_path=db)
    assert denied["result"]["ok"] is False
    assert "高风险" in str(denied["result"]["reason"])

    allowed = ip.run_closure(
        task_id, closed_by="李主任", high_risk_ack="已复核确属高风险", db_path=db
    )
    assert allowed["result"]["ok"] is True


def test_repeat_close_does_not_duplicate(db: Path) -> None:
    """已闭环任务重复操作不产生第二条记录。"""
    task_id = _seed(db)
    _approve(task_id, db)
    ip.run_closure(task_id, closed_by="李主任", db_path=db)
    again = ip.run_closure(task_id, closed_by="王主任", db_path=db)

    assert again["ok"] is True
    assert again["result"].get("idempotent") is True
    assert len(closure_store.list_closures(db_path=db)) == 1


def test_closure_does_not_modify_task_status(db: Path) -> None:
    """闭环不得修改 tasks.status（闭环是独立业务事实）。"""
    task_id = _seed(db)
    before = sqlite_db.get_task(task_id, db_path=db)["status"]
    _approve(task_id, db)
    ip.run_closure(task_id, closed_by="李主任", db_path=db)
    assert sqlite_db.get_task(task_id, db_path=db)["status"] == before


def test_submitted_is_not_closed(db: Path) -> None:
    """「已提交」不得等同于「已闭环」。"""
    task_id = _seed(db)
    assert submission_store.get_submission(task_id, db_path=db) is not None
    assert closure_store.is_closed(task_id, db_path=db) is False
    assert closure_svc.get_closure_status(task_id, db_path=db)["closed"] is False


def test_new_submission_invalidates_old_verification(db: Path) -> None:
    """重新提交后旧核验结论不得用于闭环。"""
    task_id = _seed(db)
    _approve(task_id, db)
    task_dispatch.submit_work(task_id, content="换了一份新内容", db_path=db)
    c = ip.run_closure(task_id, closed_by="李主任", db_path=db)
    assert c["result"]["ok"] is False
    assert closure_store.is_closed(task_id, db_path=db) is False

# ============================================================
# 九、UI 缺陷回归（本轮修复项）
# ============================================================


def test_submission_status_is_rendered_in_chinese(db: Path) -> None:
    """回归：办理结果必须显示中文状态，而不是「—」。

    缺陷成因：把整条 submission 记录（dict）传给了 submission_label()，
    取不到 status 键，映射必然落空。修复后传入 status 字段。
    """
    from ui import labels
    from database import submission_store

    task_id = _seed(db)
    submission = submission_store.get_submission(task_id, db_path=db)
    assert submission["status"] == "submitted"

    # 正确用法：传 status
    assert labels.submission_label(submission.get("status")) == "已提交"
    # 错误用法的反证：传 dict 会落空（本缺陷的根因）
    assert labels.submission_label(submission) == labels.UNKNOWN_TEXT


def test_scope_filter_distinguishes_closed_tasks(db: Path) -> None:
    """回归：已闭环任务必须能在页面上被查到，不能从下拉里消失。

    缺陷成因：_submittable_tasks() 把已闭环任务直接排除，
    闭环后用户在页面上再也看不到闭环时间与闭环人。
    """
    from ui.pages import inspection_page as page

    all_tasks = page.sqlite_db.list_tasks
    try:
        page.sqlite_db.list_tasks = lambda: [{"id": 1}, {"id": 2}, {"id": 3}]
        closed = {2}
        assert [t["id"] for t in page._tasks_in_scope("待办理", closed)] == [1, 3]
        assert [t["id"] for t in page._tasks_in_scope("已闭环", closed)] == [2]
        assert [t["id"] for t in page._tasks_in_scope("全部", closed)] == [1, 2, 3]
    finally:
        page.sqlite_db.list_tasks = all_tasks


def test_empty_hints_cover_all_scopes() -> None:
    """三种范围都要有对应的空状态文案，不能复用同一句误导用户。"""
    from ui.pages import inspection_page as page

    titles = {s: page._empty_title_for(s) for s in ("待办理", "已闭环", "全部")}
    assert titles["已闭环"] == "暂无已闭环任务"
    assert titles["待办理"] == "暂无可检查的任务"
    assert len(set(page._empty_hint_for(s) for s in titles)) == 3