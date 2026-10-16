"""2.1 网评冲刺 P0-A：闭环数据一致性（驾驶舱统计口径对齐闭环权威）。

权威来源纪律
------------
正式办结以 ``task_closures`` 为唯一权威；闭环**不回写** tasks.status
（``tests/test_v22_closure.py::test_08b`` 锁定的架构决策，本文件不改该行为）。
本文件验证：驾驶舱所有「需要关注」类计数都显式咨询该权威，与同页
``collect_findings``、``execution_check`` 督办清单保持同一口径。

覆盖场景
--------
    1. reminder_agent / report_agent 的 exclude_task_ids（缺省行为不变）；
    2. dashboard 页面计数助手接受排除集且保持原签名兼容；
    3. 「已闭环但 status 未完成」的逾期/临期任务：不计入需要关注，
       不出现在「AI 主动发现」清单，督办清单同样排除；
    4. 闭环链路完整性与幂等（重复闭环、闭环后反馈/下发被拒）；
    5. AppTest 真实渲染：驾驶舱 banner 数字与清单一致。

安全约定：全部使用 tmp_path 隔离数据库；LLM 替换为抛错桩；
不触碰真实演示库与开发库。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent import reminder_agent, report_agent
from database import (
    closure_store,
    models,
    sqlite_db,
    task_metadata_store,
    verification_store,
)
from tools import (
    closure_service,
    execution_check,
    task_dispatch,
    verification_service,
)
from ui.pages import dashboard_page as dp

pytest.importorskip("streamlit")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

TODAY = date.today()


# ============================================================
# 夹具与工具
# ============================================================


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """隔离数据库（显式 db_path 传递，不依赖模块全局）。"""
    path = tmp_path / "p0_closure.db"
    sqlite_db.init_db(path)
    return path


def _task_payload(
    title: str,
    *,
    deadline: str,
    owner: str = "王老师",
    department: str = "保卫处",
    risk: str = "green",
    need_confirm: bool = False,
) -> dict[str, Any]:
    return {
        "title": title,
        "department": department,
        "owner": owner,
        "deadline": deadline,
        "priority": "medium",
        "status": "pending",
        "risk_level": risk,
        "need_confirm": need_confirm,
        "description": f"{title}（测试用）",
    }


def _create(db: Path, title: str, *, deadline: str, **kwargs: Any) -> int:
    return int(
        sqlite_db.create_task(_task_payload(title, deadline=deadline, **kwargs), db_path=db)["id"]
    )


def _run_full_chain_to_closure(
    db: Path,
    task_id: int,
    *,
    with_in_progress_feedback: bool = True,
) -> dict[str, Any]:
    """走完整真实链路：下发 →（进行中反馈）→ 回执 → 办结标准 → 核验通过 → 正式闭环。"""
    task_dispatch.dispatch_task(task_id, db_path=db)
    if with_in_progress_feedback:
        outcome = execution_check.save_feedback(
            task_id, status="in_progress", note="正在办理", db_path=db
        )
        assert outcome["ok"] is True
    task_dispatch.submit_work(task_id, "办理结果：已完成排查并提交台账", db_path=db)
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交办理结果", "完成隐患销号"],
        closure_source=models.CLOSURE_SOURCE_MATERIAL,
        closure_confirmed=True,
        db_path=db,
    )
    verification_service.verify_submission(
        task_id, decision=models.VERIFICATION_APPROVED, reason="材料齐备", db_path=db
    )
    records = verification_store.list_verifications(task_id, db_path=db)
    assert records[-1]["status"] == models.VERIFICATION_APPROVED
    closed = closure_service.close_task(
        task_id, closed_by="李主任", closure_note="测试闭环", db_path=db
    )
    assert closed["ok"] is True and closed["idempotent"] is False
    return closed


# ============================================================
# 一、reminder_agent / report_agent 的排除参数
# ============================================================


def _injected_tasks() -> list[dict[str, Any]]:
    return [
        {"id": 1, "title": "甲", "status": "processing", "deadline": (TODAY + timedelta(days=1)).isoformat()},
        {"id": 2, "title": "乙", "status": "processing", "deadline": (TODAY + timedelta(days=1)).isoformat()},
        {"id": 3, "title": "丙", "status": "processing", "deadline": (TODAY + timedelta(days=30)).isoformat()},
    ]


def test_reminder_exclude_task_ids_filters_scan() -> None:
    """exclude_task_ids 精确排除指定任务；缺省 None / 空集合时行为不变。

    注意：check_all 返回**全部有截止日期**的任务（含 normal 级），
    「需要关注」的裁剪由调用方按等级求和完成——本测试只验证排除机制。
    """
    tasks = _injected_tasks()

    default_items = reminder_agent.check_all(tasks=tasks, today=TODAY)
    assert {item["task_id"] for item in default_items} == {1, 2, 3}

    excluded_items = reminder_agent.check_all(tasks=tasks, today=TODAY, exclude_task_ids={1})
    assert {item["task_id"] for item in excluded_items} == {2, 3}

    assert reminder_agent.check_all(tasks=tasks, today=TODAY, exclude_task_ids=set()) == default_items
    assert reminder_agent.check_all(tasks=tasks, today=TODAY, exclude_task_ids=None) == default_items


def test_reminder_count_levels_and_messages_respect_exclusion() -> None:
    """count_levels / generate_messages 同样支持排除。"""
    tasks = _injected_tasks()

    assert reminder_agent.count_levels(tasks=tasks, today=TODAY)["urgent"] == 2
    assert reminder_agent.count_levels(tasks=tasks, today=TODAY, exclude_task_ids={1, 2})["urgent"] == 0
    assert reminder_agent.count_levels(tasks=tasks, today=TODAY, exclude_task_ids={1})["urgent"] == 1

    # 文案列表同样只排除指定任务（normal 级的丙不受影响）
    assert len(reminder_agent.generate_messages(tasks=tasks, today=TODAY)) == 3
    assert len(reminder_agent.generate_messages(tasks=tasks, today=TODAY, exclude_task_ids={1, 2})) == 1


def test_report_agent_dashboard_exclusion(tmp_path: Path) -> None:
    """DB 级验证：排除后 urgent 下降、total 等普查口径不变；缺参与行为不变。"""
    path = tmp_path / "dash.db"
    sqlite_db.init_db(path)
    _create(path, "临期任务", deadline=(TODAY + timedelta(days=1)).isoformat())
    _create(path, "普通任务", deadline=(TODAY + timedelta(days=30)).isoformat())
    near_id = int(sqlite_db.list_tasks(db_path=path)[0]["id"])

    default = report_agent.generate_dashboard(today=TODAY, db_path=path)
    assert default["total"] == 2
    assert default["urgent"] == 1

    excluded = report_agent.generate_dashboard(today=TODAY, db_path=path, exclude_task_ids={near_id})
    assert excluded["total"] == 2, "总任务数等普查口径不得因排除而变化"
    assert excluded["urgent"] == 0
    assert excluded["normal"] == 1


# ============================================================
# 二、驾驶舱页面助手（签名兼容 + 排除生效）
# ============================================================


def test_dashboard_helpers_signature_compat_and_exclusion(tmp_path: Path) -> None:
    """_count_risk/_count_need_confirm：原签名可用，新增排除参数生效。"""
    path = tmp_path / "helpers.db"
    sqlite_db.init_db(path)
    red_id = _create(path, "高风险任务", deadline="2099-12-31", risk="red")
    _create(path, "普通任务", deadline="2099-12-31", need_confirm=True)

    # 既有调用方式（位置参数 + db_path 关键字）必须保持可用
    assert dp._count_risk("red", db_path=path) == 1
    assert dp._count_need_confirm(db_path=path) == 1

    # 新增排除参数
    assert dp._count_risk("red", db_path=path, exclude_task_ids={red_id}) == 0
    assert dp._count_need_confirm(db_path=path, exclude_task_ids={red_id}) == 1
    assert dp._count_risk("red", db_path=path, exclude_task_ids=set()) == 1


# ============================================================
# 三、核心场景：已闭环但 status 未完成的任务
# ============================================================


def test_closed_task_keeps_status_and_is_excluded_everywhere(db: Path) -> None:
    """闭环不回写 status；驾驶舱计数、发现清单、督办清单三处口径一致排除。"""
    closed_id = _create(db, "已闭环的临期任务", deadline=(TODAY + timedelta(days=1)).isoformat())
    _run_full_chain_to_closure(db, closed_id)

    # 架构事实：闭环记录已产生，但 tasks.status 未被同步改写
    assert closure_store.is_closed(closed_id, db_path=db) is True
    assert sqlite_db.get_task(closed_id, db_path=db)["status"] == "pending"

    closed_ids = {int(item["task_id"]) for item in closure_store.list_closures(db_path=db)}
    assert closed_ids == {closed_id}

    # 1) 驾驶舱「需要关注」类计数：默认口径含它，排除后不含（与清单同基准）
    assert reminder_agent.count_levels(today=TODAY, db_path=db)["urgent"] == 1
    assert reminder_agent.count_levels(today=TODAY, db_path=db, exclude_task_ids=closed_ids)["urgent"] == 0
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=db)
    assert dashboard["urgent"] == 1 and dashboard["total"] == 1
    dashboard_closed_aware = report_agent.generate_dashboard(
        today=TODAY, db_path=db, exclude_task_ids=closed_ids
    )
    assert dashboard_closed_aware["urgent"] == 0

    # 2) 「AI 主动发现」清单：不含已闭环任务
    findings = dp.collect_findings(db_path=db)
    assert findings == [], "已正式闭环的任务不得出现在主动发现清单"

    # 3) 持续督办清单（第四页同源）：同样排除
    assert execution_check.supervision_items(today=TODAY, db_path=db) == []
    overview = execution_check.build_overview(
        execution_check.build_task_rows(today=TODAY, db_path=db)
    )
    assert overview["closed"] == 1


def test_closed_overdue_task_not_counted_as_attention(db: Path) -> None:
    """评委可见场景：闭环的逾期任务不被算作「需要关注」，也不再进督办。"""
    closed_id = _create(db, "已闭环的逾期任务", deadline=(TODAY - timedelta(days=3)).isoformat())
    _run_full_chain_to_closure(db, closed_id)
    open_id = _create(db, "未办的逾期任务", deadline=(TODAY - timedelta(days=2)).isoformat())

    closed_ids = {int(item["task_id"]) for item in closure_store.list_closures(db_path=db)}

    # 默认（旧行为）：两个逾期都被数进去 → 与清单矛盾（本次修复点）
    assert reminder_agent.count_levels(today=TODAY, db_path=db)["overdue"] == 2
    # 闭环感知（新口径）：只剩未办的那一个
    assert reminder_agent.count_levels(today=TODAY, db_path=db, exclude_task_ids=closed_ids)["overdue"] == 1

    findings = dp.collect_findings(db_path=db)
    assert [f["task_id"] for f in findings] == [open_id], "清单只应包含未办结的逾期任务"

    supervision = execution_check.supervision_items(today=TODAY, db_path=db)
    assert [item["task_id"] for item in supervision] == [open_id]


# ============================================================
# 四、闭环链路完整性与幂等
# ============================================================


def test_closure_chain_integrity_and_idempotency(db: Path) -> None:
    """重复闭环不产生第二份记录；闭环后反馈被拒、再下发被拒。"""
    task_id = _create(db, "链路任务", deadline=(TODAY + timedelta(days=5)).isoformat())
    _run_full_chain_to_closure(db, task_id)

    assert len(closure_store.list_closures(db_path=db)) == 1

    # 重复触发闭环：幂等返回，不产生第二份记录
    again = closure_service.close_task(task_id, closed_by="王主任", db_path=db)
    assert again["ok"] is True and again["idempotent"] is True
    assert len(closure_store.list_closures(db_path=db)) == 1

    # 闭环后不允许再录入执行反馈（闭环保护）
    feedback = execution_check.save_feedback(
        task_id, status="in_progress", note="闭环后补反馈", db_path=db
    )
    assert feedback["ok"] is False
    assert execution_check.CLOSED_REASON in feedback["reason"]

    # 闭环后不允许再次下发
    with pytest.raises(task_dispatch.NotReadyError):
        task_dispatch.dispatch_task(task_id, db_path=db)

    # 全链路记录齐全且都挂在同一个 task_id 上
    logs = sqlite_db.get_logs(task_id, db_path=db)
    actions = [log["action"] for log in logs]
    for expected in (
        models.ACTION_CREATE,
        models.ACTION_DISPATCH,
        models.ACTION_SUBMIT,
        models.ACTION_VERIFY,
        models.ACTION_CLOSE,
    ):
        assert expected in actions
    assert len(verification_store.list_verifications(task_id, db_path=db)) >= 1


def test_closure_blocked_without_verification(db: Path) -> None:
    """未核验 / 核验不通过 / 无办结标准时，正式闭环必须被后端拒绝。"""
    # 场景 1：无回执无核验（首道闸门是办结标准缺失）
    task_id = _create(db, "未提交任务", deadline="2099-12-31")
    result = closure_service.close_task(task_id, closed_by="李主任", db_path=db)
    assert result["ok"] is False and "办结标准" in result["reason"]

    # 场景 2：有回执有标准但核验结论为 need_supplement
    task_dispatch.dispatch_task(task_id, db_path=db)
    task_dispatch.submit_work(task_id, "办理结果", db_path=db)
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交办理结果"],
        closure_source=models.CLOSURE_SOURCE_MATERIAL,
        closure_confirmed=True,
        db_path=db,
    )
    verification_service.verify_submission(
        task_id, decision=models.VERIFICATION_NEED_SUPPLEMENT, reason="材料不完整", db_path=db
    )
    blocked = closure_service.close_task(task_id, closed_by="李主任", db_path=db)
    assert blocked["ok"] is False and "未解决结论" in blocked["reason"]
    assert closure_store.is_closed(task_id, db_path=db) is False

    # 场景 3：办结标准未经确认
    plain_id = _create(db, "无标准任务", deadline="2099-12-31")
    task_dispatch.dispatch_task(plain_id, db_path=db)
    task_dispatch.submit_work(plain_id, "办理结果", db_path=db)
    task_metadata_store.set_closure_criteria(
        plain_id,
        ["提交办理结果"],
        closure_source=models.CLOSURE_SOURCE_MATERIAL,
        closure_confirmed=False,
        db_path=db,
    )
    verification_service.verify_submission(plain_id, decision=models.VERIFICATION_APPROVED, db_path=db)
    blocked2 = closure_service.close_task(plain_id, closed_by="李主任", db_path=db)
    assert blocked2["ok"] is False and "确认" in blocked2["reason"]


def test_closure_blocked_for_high_risk_without_ack(db: Path) -> None:
    """高风险任务必须显式授权才能闭环。"""
    task_id = _create(
        db, "高风险任务", deadline=(TODAY + timedelta(days=5)).isoformat(), risk="red"
    )
    task_dispatch.dispatch_task(task_id, db_path=db)
    task_dispatch.submit_work(task_id, "办理结果", db_path=db)
    task_metadata_store.set_closure_criteria(
        task_id,
        ["提交办理结果"],
        closure_source=models.CLOSURE_SOURCE_MATERIAL,
        closure_confirmed=True,
        db_path=db,
    )
    verification_service.verify_submission(task_id, decision=models.VERIFICATION_APPROVED, db_path=db)

    denied = closure_service.close_task(task_id, closed_by="李主任", db_path=db)
    assert denied["ok"] is False and "高风险" in denied["reason"]
    assert closure_store.is_closed(task_id, db_path=db) is False

    allowed = closure_service.close_task(
        task_id, closed_by="李主任", high_risk_ack="已完成现场复核", db_path=db
    )
    assert allowed["ok"] is True


# ============================================================
# 五、AppTest 真实渲染：banner 与清单口径一致
# ============================================================


@pytest.fixture()
def app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AppTest 隔离库 + LLM 抛错桩（仿 test_v22_ui_browser 模式）。"""
    path = tmp_path / "app_p0.db"
    sqlite_db.init_db(path)
    monkeypatch.setattr(sqlite_db, "DB_PATH", path)

    from llm import client

    def _no_llm(*args: object, **kwargs: object) -> object:
        raise client.LLMError("本测试不调用真实大模型")

    monkeypatch.setattr(client, "chat_json", _no_llm)
    return path


def _open_app() -> AppTest:
    from config import settings

    instance = AppTest.from_file(str(settings.BASE_DIR / "app.py"), default_timeout=60)
    instance.run()
    return instance


def _markdown_text(app: AppTest) -> str:
    return "\n".join(str(item.value) for item in app.markdown)


def test_dashboard_banner_matches_findings_after_closure(app_db: Path) -> None:
    """真实渲染：闭环的临期任务不计入 banner「需要关注」，标题也不在清单出现。"""
    closed_id = _create(
        app_db, "已闭环临症任务 UniqueXYZ", deadline=(TODAY + timedelta(days=1)).isoformat()
    )
    _run_full_chain_to_closure(app_db, closed_id)
    _create(app_db, "未办临期任务", deadline=(TODAY + timedelta(days=1)).isoformat())

    app = _open_app()
    app.session_state["cf_nav_page"] = "智能驾驶舱"
    app.run()

    assert not app.exception
    text = _markdown_text(app)
    # banner：全量 2 项，排除闭环后只剩 1 项需要关注
    assert "AI 已检查 2 项事务，发现 1 项需要关注。" in text
    # 闭环任务不出现在主动发现清单，未办任务正常出现
    assert "已闭环临症任务 UniqueXYZ" not in text
    assert "未办临期任务" in text
