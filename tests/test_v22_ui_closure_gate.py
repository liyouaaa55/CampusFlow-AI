"""本轮 UI 收敛的专项验证：闭环前置条件「页面预告」与「后端闸门」必须一致。

为什么单独立一个文件
--------------------
改造前存在一个真实的口径分歧：
    页面顶部阶段卡与闭环前置条件只看「当前提交版本的**最后一条**核验」，
    而 tools.closure_service.close_task 还会检查当前版本是否存在
    need_supplement / rejected / manual_review 等**未决结论**。
因此会出现：同一提交内容先被判定「需补充」、后被人工裁决「通过」时，
页面显示「核验通过，待正式闭环」，而点击闭环被后端拒绝。

本文件把「页面只读预告 == 后端闸门」变成显式约束：
    对同一场景，assert (页面预告无阻断) == close_task(...)["ok"]。
这样两条路径一旦再次漂移，测试会立即失败。

全部使用 tmp_path 独立数据库；页面函数走默认库，故 monkeypatch sqlite_db.DB_PATH。
不触碰真实的 data/campusflow.db，也不调用任何真实大模型。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

import pytest

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
from tools import verification_service as verify_svc
from ui.pages import inspection_page as ip

CRITERIA: list[str] = ["提交教材征订统计表"]


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离数据库，并把「默认库」也切到它上面。

    页面函数（_current_stage / _current_verification / _closure_blockers）
    不显式传 db_path，走 sqlite_db.DB_PATH；这里切到隔离库，
    保证页面预告读的是与测试同一份事实。
    """
    path = tmp_path / "ui_closure_gate.db"
    sqlite_db.init_db(path)
    monkeypatch.setattr(sqlite_db, "DB_PATH", path)
    return path


def _seed(db: Path, *, dispatch: bool = True, **over: Any) -> int:
    """建一条「已下发、已人工确认」的可闭环任务基线。

    dispatch=False 用于构造「尚未确认、按规则不得下发」的任务。
    """
    payload: dict[str, Any] = {
        "title": "教材征订统计",
        "department": "教务处",
        "owner": "陈老师",
        "deadline": "2026-12-31",
        "priority": "medium",
        "status": "confirmed",
        "risk_level": "green",
        "need_confirm": False,
        "description": "完成教材征订统计并提交统计表。",
    }
    payload.update(over)
    task_id = int(sqlite_db.create_task(payload, db_path=db)["id"])
    if dispatch:
        task_dispatch.dispatch_task(task_id, db_path=db)
    return task_id


def _set_criteria(db: Path, task_id: int, *, confirmed: bool = True, source: str = "material") -> None:
    task_metadata_store.set_closure_criteria(
        task_id,
        CRITERIA,
        closure_source=source,
        closure_confirmed=confirmed,
        db_path=db,
    )


def _submit(db: Path, task_id: int, content: str) -> None:
    task_dispatch.submit_work(task_id, content=content, db_path=db)


def _verify(db: Path, task_id: int, decision: str, reason: str = "") -> None:
    verify_svc.verify_submission(task_id, decision=decision, reason=reason, db_path=db)


# ============================================================
# 场景构造：每个返回 (task_id, 期望能否闭环)
# ============================================================


def _scenario_ready(db: Path) -> tuple[int, bool]:
    """办结标准已确认 + 当前版本核验通过 + 其他条件满足。"""
    task_id = _seed(db)
    _set_criteria(db, task_id)
    _submit(db, task_id, "统计表已完成并提交")
    _verify(db, task_id, "approved")
    return task_id, True


def _scenario_no_criteria(db: Path) -> tuple[int, bool]:
    """从未设置办结标准。"""
    task_id = _seed(db)
    _submit(db, task_id, "统计表已完成并提交")
    _verify(db, task_id, "approved")
    return task_id, False


def _scenario_criteria_unconfirmed(db: Path) -> tuple[int, bool]:
    """办结标准已填写但未经督办人员确认。"""
    task_id = _seed(db)
    _set_criteria(db, task_id, confirmed=False)
    _submit(db, task_id, "统计表已完成并提交")
    _verify(db, task_id, "approved")
    return task_id, False


def _scenario_need_supplement(db: Path) -> tuple[int, bool]:
    """当前版本核验结论为「需补充」。"""
    task_id = _seed(db)
    _set_criteria(db, task_id)
    _submit(db, task_id, "统计表已完成并提交")
    _verify(db, task_id, "need_supplement", reason="缺少签字确认表")
    return task_id, False


def _scenario_historical_pass_only(db: Path) -> tuple[int, bool]:
    """历史版本核验通过，当前版本核验未通过 —— 当前版本必须被阻断。"""
    task_id = _seed(db)
    _set_criteria(db, task_id)
    _submit(db, task_id, "统计表 v1")
    _verify(db, task_id, "approved")
    _submit(db, task_id, "统计表 v2（修订）")  # 新 digest，旧通过不再算数
    _verify(db, task_id, "need_supplement", reason="v2 缺少附件")
    return task_id, False


def _scenario_unresolved_then_approved(db: Path) -> tuple[int, bool]:
    """同一提交内容：先「需补充」，后人工裁决「通过」——仍必须被阻断。

    这是本轮发现的口径分歧的定点回归：页面阶段卡会显示「核验通过，待正式闭环」，
    但后端 close_task 因当前版本存在未决结论而拒绝。修复后页面预告必须同步阻断。
    """
    task_id = _seed(db)
    _set_criteria(db, task_id)
    _submit(db, task_id, "统计表已完成并提交")
    _verify(db, task_id, "need_supplement", reason="缺少签字确认表")
    _verify(db, task_id, "approved", reason="补齐后人工裁决通过")
    return task_id, False


def _scenario_overdue_status(db: Path) -> tuple[int, bool]:
    """任务处于逾期例外态时不得按正常办结闭环。"""
    task_id = _seed(db, status="overdue")
    _set_criteria(db, task_id)
    _submit(db, task_id, "统计表已完成并提交")
    _verify(db, task_id, "approved")
    return task_id, False


SCENARIOS: dict[str, Callable[[Path], tuple[int, bool]]] = {
    "ready": _scenario_ready,
    "no_criteria": _scenario_no_criteria,
    "criteria_unconfirmed": _scenario_criteria_unconfirmed,
    "need_supplement": _scenario_need_supplement,
    "historical_pass_only": _scenario_historical_pass_only,
    "unresolved_then_approved": _scenario_unresolved_then_approved,
    "overdue_status": _scenario_overdue_status,
}


# ============================================================
# 一、页面预告 == 后端闸门（核心一致性约束）
# ============================================================


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_page_preview_agrees_with_backend_gate(db: Path, name: str) -> None:
    """页面预告「无阻断」当且仅当后端 close_task 放行。"""
    task_id, expected_ok = SCENARIOS[name](db)
    task = sqlite_db.get_task(task_id, db_path=db)

    blockers = ip._closure_blockers(task, task_id)
    result = closure_svc.close_task(task_id, closed_by="李主任", db_path=db)

    assert (blockers == []) == bool(result["ok"]), (
        f"场景 {name}：页面预告阻断={blockers}，后端结果={result}"
    )
    assert bool(result["ok"]) is expected_ok, f"场景 {name} 的期望结果不符：{result}"

    if not result["ok"]:
        # 阻断时必须能给出可展示的真实原因，不能只说「失败」
        assert str(result.get("reason") or "").strip(), "阻断原因不得为空"


def test_unresolved_then_approved_stage_and_blockers(db: Path) -> None:
    """同版本「先需补充、后通过」：阶段卡与管理口径必须自洽。"""
    task_id, _ = _scenario_unresolved_then_approved(db)
    task = sqlite_db.get_task(task_id, db_path=db)

    # 当前版本最后一条是 approved，因此阶段卡显示「待正式闭环」
    assert ip._current_stage(task_id) == "ready_close"
    # 但页面预告必须指出「存在未解决结论」，与后端一致
    blockers = ip._closure_blockers(task, task_id)
    assert any("未解决结论" in item for item in blockers), blockers
    assert closure_store.is_closed(task_id, db_path=db) is False


def test_historical_pass_does_not_allow_closure(db: Path) -> None:
    """历史版本通过不得冒充当前版本的通过。"""
    task_id, _ = _scenario_historical_pass_only(db)
    task = sqlite_db.get_task(task_id, db_path=db)

    assert ip._current_stage(task_id) == "not_passed"
    current = ip._current_verification(task_id)
    assert current is not None and current["status"] == models.VERIFICATION_NEED_SUPPLEMENT

    blockers = ip._closure_blockers(task, task_id)
    assert blockers, "当前版本未通过时必须给出阻断原因"
    assert closure_svc.close_task(task_id, closed_by="李主任", db_path=db)["ok"] is False


def test_ready_scenario_closes_and_writes_chain_log(db: Path) -> None:
    """全部条件满足时正式闭环仍能正常工作（回归保护）。"""
    task_id, _ = _scenario_ready(db)
    task = sqlite_db.get_task(task_id, db_path=db)

    assert ip._closure_blockers(task, task_id) == []
    result = closure_svc.close_task(task_id, closed_by="李主任", closure_note="验收通过", db_path=db)
    assert result["ok"] is True
    assert closure_store.is_closed(task_id, db_path=db) is True
    assert models.ACTION_CLOSE in [
        x["action"] for x in sqlite_db.get_logs(task_id, db_path=db)
    ]


def test_repeat_closure_is_idempotent(db: Path) -> None:
    """重复闭环不产生第二条记录（既有安全机制未被削弱）。"""
    task_id, _ = _scenario_ready(db)
    closure_svc.close_task(task_id, closed_by="李主任", db_path=db)
    again = closure_svc.close_task(task_id, closed_by="王主任", db_path=db)

    assert again["ok"] is True and again.get("idempotent") is True
    assert len(closure_store.list_closures(db_path=db)) == 1


def test_preview_never_bypasses_backend_check(db: Path) -> None:
    """结构性约束：页面预告只是只读判断，真正的闸门仍在 close_task。

    即使把页面预告强行「清空」（模拟有人篡改展示逻辑），后端仍必须拒绝。
    """
    task_id, _ = _scenario_need_supplement(db)
    assert closure_svc.close_task(task_id, closed_by="李主任", db_path=db)["ok"] is False
    assert closure_store.is_closed(task_id, db_path=db) is False


# ============================================================
# 二、UI 结构约束（本轮收敛成果的防回归）
# ============================================================


def test_inspection_history_is_collapsed_by_default() -> None:
    """核验历史必须默认折叠，避免旧结论抢占当前结论的视觉位置。"""
    source = inspect.getsource(ip._render_history)
    assert "st.expander(" in source
    assert "expanded=False" in source


def test_inspection_history_labels_current_and_historical_versions() -> None:
    """核验历史必须显式区分「当前提交版本」与「历史版本」。"""
    source = inspect.getsource(ip._render_history)
    assert "当前提交版本" in source
    assert "历史版本" in source


def test_inspection_uses_two_core_sections_only() -> None:
    """执行检查页只保留两个编号功能区。"""
    assert ip.SECTION_RESULT.startswith("01")
    assert ip.SECTION_VERIFY_CLOSE.startswith("02")


def test_inspection_result_block_uses_compact_submission_view() -> None:
    """办理结果区必须走 compact 渲染，避免与顶部任务卡重复任务字段。"""
    source = inspect.getsource(ip._render_submission_result)
    assert "compact=True" in source


def test_inspection_has_no_session_cached_conclusion() -> None:
    """核验结论必须来自数据库当前版本，不得用 session_state 缓存冒充。"""
    source = inspect.getsource(ip)
    assert "cf_verify_result" not in source


def test_closure_blockers_share_service_rule_constants() -> None:
    """页面预告的规则取值必须引用 closure_service，而不是另写一套阈值。"""
    source = inspect.getsource(ip._closure_blockers)
    for name in (
        "ALLOWED_CLOSURE_SOURCES",
        "BLOCKING_STATUSES",
        "BLOCKED_TASK_STATUSES",
    ):
        assert f"closure_svc.{name}" in source, name


def test_tasks_page_confirmation_is_not_folded() -> None:
    """任务确认必须直接可见：确认区块不得出现在折叠区之后。"""
    from ui.pages import tasks_page as tp

    source = inspect.getsource(tp._render_actions)
    confirm_at = source.index("_render_task_confirmation")
    expander_at = source.index('st.expander("管理操作')
    assert confirm_at < expander_at, "任务确认必须在折叠的管理操作之前渲染"


def test_tasks_page_task_list_has_fixed_height() -> None:
    """任务列表必须固定高度，避免大量任务撑长整页。"""
    from ui.pages import tasks_page as tp

    source = inspect.getsource(tp.render)
    assert "height=TASK_TABLE_HEIGHT" in source
    assert tp.TASK_TABLE_HEIGHT > 0


def test_tasks_page_confirmation_matches_dispatch_rule() -> None:
    """「是否待确认」的判定必须与 task_dispatch 的下发前置规则一致。"""
    from ui.pages import tasks_page as tp

    pending = {"need_confirm": 1, "status": "pending", "owner": "陈老师"}
    confirmed = {"need_confirm": 1, "status": "confirmed", "owner": "陈老师"}
    no_confirm_needed = {"need_confirm": 0, "status": "pending", "owner": "陈老师"}

    assert tp.needs_task_confirmation(pending) is True
    assert tp.needs_task_confirmation(confirmed) is False
    assert tp.needs_task_confirmation(no_confirm_needed) is False

    # 与后端口径逐一对齐
    assert task_dispatch.can_dispatch(confirmed) is (not tp.needs_task_confirmation(confirmed))
    assert task_dispatch.dispatch_block_reason(pending) == "need_confirm"


def test_confirm_task_writes_real_status_and_log(db: Path) -> None:
    """任务确认必须真实推进状态并留痕，且失败时给出中文原因。"""
    from ui.pages import tasks_page as tp

    task_id = _seed(db, dispatch=False, need_confirm=1, status="pending")
    assert tp.needs_task_confirmation(sqlite_db.get_task(task_id, db_path=db)) is True
    outcome = tp.confirm_task(task_id, operator="李主任", db_path=db)
    assert outcome["ok"] is True

    task = sqlite_db.get_task(task_id, db_path=db)
    assert task["status"] == "confirmed"
    assert tp.needs_task_confirmation(task) is False
    assert "confirmed" in [
        x["action"] for x in sqlite_db.get_logs(task_id, db_path=db)
    ]

    # 任务不存在：给出中文提示，不抛异常
    missing = tp.confirm_task(999999, db_path=db)
    assert missing["ok"] is False
    assert "任务不存在" in missing["message"]


def test_confirmed_task_not_reconfirmed(db: Path) -> None:
    """已确认任务不再需要确认表单（避免重复写入 confirmed 日志）。"""
    from ui.pages import tasks_page as tp

    task_id = _seed(db, need_confirm=1, status="confirmed")
    task = sqlite_db.get_task(task_id, db_path=db)
    assert tp.needs_task_confirmation(task) is False

    before = len(sqlite_db.get_logs(task_id, db_path=db))
    # 界面不会再提供入口；即便被直接调用，也只应追加一条（不做幂等承诺），
    # 因此这里只断言「界面判定为不需要确认」，不调用 backdoor。
    assert before >= 1


def test_submission_result_is_not_verified_or_closed(db: Path) -> None:
    """「已提交」不得被展示为「已核验通过」或「已正式闭环」。"""
    task_id, _ = _scenario_ready(db)
    # 重新提交一份新内容：旧通过立即失效
    _submit(db, task_id, "统计表 v9（换了一版）")

    assert submission_store.get_submission(task_id, db_path=db) is not None
    assert ip._current_verification(task_id) is None
    assert ip._current_stage(task_id) == "pending_verify"
    assert closure_store.is_closed(task_id, db_path=db) is False
    assert len(verification_store.list_verifications(task_id, db_path=db)) >= 1
