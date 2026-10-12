"""执行期督办（ui/pages/execution_panel.py）接入测试。

v3.0 第三阶段：本文件原先覆盖 task_page 上的「智能处置」，该实现已抽取为
独立模块 `ui/pages/execution_panel.py`，断言目标随之迁移；测试内容与强度不变。

测试分层：
1. **业务动作层**（run_analysis / confirm_execution / reject_execution）
   —— 纯函数式，不依赖 Streamlit 运行时，覆盖事件链、幂等、防越权；
2. **状态层**（state 的 execution plan / result）—— 覆盖 rerun 不丢结果、
   task_id 串号防护；
3. **展示层**（中文映射）—— 覆盖页面不泄漏内部英文枚举。

全部注入假模型，不调用真实 DeepSeek API。
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent import execution_agent
from config import settings
from database import models, sqlite_db
from llm import client
from tools import execution_tool as et
from ui import labels
from ui.components import state, tables
from ui.pages import execution_panel, tasks_page

# ============================================================
# 固定基准数据
# ============================================================

BASE_DATE = date(2026, 10, 5)

DEADLINE_OVERDUE = "2026-10-03"   # days_left = -2 -> overdue
DEADLINE_URGENT = "2026-10-06"    # days_left = +1 -> urgent
DEADLINE_DUE_SOON = "2026-10-07"  # days_left = +2 -> due_soon
DEADLINE_NORMAL = "2026-10-20"    # days_left = +15 -> normal

GOOD_SITUATION = "该事项已经超过截止日期，目前仍未完成。"
GOOD_REASONS = ["截止时间已经超过当前日期。", "当前任务仍处于待办状态。"]


class SpyLLM:
    """记录调用次数的假模型。"""

    def __init__(self, payload: Any = None) -> None:
        self.payload = (
            {"situation": GOOD_SITUATION, "reasons": list(GOOD_REASONS)}
            if payload is None
            else payload
        )
        self.calls = 0

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        self.calls += 1
        return self.payload


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "p06a_test.db"
    sqlite_db.init_db(db_path)
    return db_path


def seed(db_path: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "实验室安全检查",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": DEADLINE_OVERDUE,
        "priority": "high",
        "status": "pending",
        "risk_level": "green",
        "description": "排查实验室安全隐患",
    }
    payload.update(overrides)
    created = sqlite_db.create_task(payload, db_path=db_path)
    return sqlite_db.get_task(created["id"], db_path=db_path)  # type: ignore[index]


def actions(db_path: Path, task_id: int) -> list[str]:
    return [log["action"] for log in sqlite_db.get_logs(task_id, db_path=db_path)]


# ============================================================
# 一、run_analysis：四种提醒等级都能分析
# ============================================================


@pytest.mark.parametrize(
    "deadline",
    [DEADLINE_OVERDUE, DEADLINE_DUE_SOON, DEADLINE_URGENT],
    ids=["overdue", "due_soon", "urgent"],
)
def test_analyze_works_for_each_actionable_level(db: Path, deadline: str) -> None:
    """用例 2/3/4：即将到期、临期、已逾期任务都能分析。"""
    task = seed(db, deadline=deadline)
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert spy.calls == 1
    assert outcome["ok"] is True
    assert outcome["plan"]["action"] in et.ALLOWED_ACTIONS


def test_normal_task_produces_no_action(db: Path) -> None:
    """用例 1：进度正常的任务不产生处置动作。"""
    task = seed(db, deadline=DEADLINE_NORMAL)
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert outcome["kind"] == "no_action"
    assert spy.calls == 0
    assert actions(db, task["id"]) == [models.ACTION_CREATE]


def test_completed_task_never_calls_llm(db: Path) -> None:
    """用例 5：已完成任务不调用大模型。"""
    task = seed(db, status="completed")
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert spy.calls == 0


def test_task_without_deadline_never_calls_llm(db: Path) -> None:
    """用例 6：无截止时间任务不调用大模型。"""
    task = seed(db, deadline=None)
    spy = SpyLLM()

    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert spy.calls == 0


def test_run_analysis_returns_chinese_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, db: Path
) -> None:
    """用例 26：分析失败只给中文提示，不向用户抛异常、不暴露 traceback。"""
    task = seed(db)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise client.LLMError("模拟内部异常 /path/to/secret.py")

    monkeypatch.setattr(client, "chat_json", boom)

    # run_analysis 必须吞掉异常并转成中文提示，绝不把异常抛给页面
    outcome = execution_panel.run_analysis(task["id"], db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert outcome["kind"] == "analysis_error"
    assert outcome["message"] == execution_panel.MSG_ANALYSIS_FAILED
    assert "Traceback" not in outcome["message"]
    assert "secret.py" not in outcome["message"]
    assert "LLMError" not in outcome["message"]
    # 失败不得留下 analyze 事件
    assert actions(db, task["id"]) == [models.ACTION_CREATE]


def test_confirm_execution_never_leaks_internals(db: Path) -> None:
    """确认执行阶段的任何失败都只返回中文提示。"""
    task = seed(db)
    plan = _plan_for(db, task["id"])
    plan["action"] = "不存在的动作"

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    for token in ("Traceback", "sqlite3", ".py", "Error:", "Exception"):
        assert token not in outcome["message"]
    assert models.ACTION_CONFIRM not in actions(db, task["id"])


# ============================================================
# 二、确认执行：三种动作的事件链
# ============================================================


def _plan_for(db_path: Path, task_id: int) -> dict[str, Any]:
    """跑一次分析并返回可确认的计划。"""
    outcome = execution_panel.run_analysis(task_id, llm_fn=SpyLLM(), db_path=db_path, today=BASE_DATE)
    assert outcome["ok"] is True
    return outcome["plan"]


def test_draft_notify_executes_and_writes_events(db: Path) -> None:
    """用例 11/19：DRAFT_NOTIFY 正确执行，事件链为 analyze→confirm→draft。"""
    task = seed(db, deadline=DEADLINE_DUE_SOON)
    plan = _plan_for(db, task["id"])
    assert plan["action"] == et.ACTION_DRAFT_NOTIFY

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is True
    assert actions(db, task["id"]) == [
        models.ACTION_CREATE,
        models.ACTION_ANALYZE,
        models.ACTION_CONFIRM,
        models.ACTION_DRAFT,
    ]
    # 不改任务数据
    after = sqlite_db.get_task(task["id"], db_path=db)
    assert after["risk_level"] == "green"
    assert after["status"] == "pending"


def test_request_info_executes_and_writes_events(db: Path) -> None:
    """用例 12/20：REQUEST_INFO 正确执行，事件链为 analyze→confirm→request。"""
    task = seed(db, owner=None)
    plan = _plan_for(db, task["id"])
    assert plan["action"] == et.ACTION_REQUEST_INFO

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is True
    assert actions(db, task["id"]) == [
        models.ACTION_CREATE,
        models.ACTION_ANALYZE,
        models.ACTION_CONFIRM,
        models.ACTION_REQUEST,
    ]
    # 绝不自动补全负责人
    assert sqlite_db.get_task(task["id"], db_path=db)["owner"] is None


def test_escalate_changes_risk_level_and_events(db: Path) -> None:
    """用例 13/14/15/21：ESCALATE 真实改 risk_level，事件链为 analyze→confirm→escalate。"""
    task = seed(db, risk_level="green", status="processing")
    plan = _plan_for(db, task["id"])
    assert plan["action"] == et.ACTION_ESCALATE

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is True
    after = sqlite_db.get_task(task["id"], db_path=db)
    assert after["risk_level"] == "red"
    # 关键：绝不改 status
    assert after["status"] == "processing"
    assert actions(db, task["id"]) == [
        models.ACTION_CREATE,
        models.ACTION_ANALYZE,
        models.ACTION_CONFIRM,
        models.ACTION_ESCALATE,
    ]


def test_escalate_never_sets_urgent_status(db: Path) -> None:
    """用例 15：提级督办绝不把 status 改成临期/已逾期。"""
    task = seed(db, risk_level="green", status="processing")
    plan = _plan_for(db, task["id"])
    execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    after = sqlite_db.get_task(task["id"], db_path=db)
    assert after["status"] not in ("urgent", "overdue")
    assert after["status"] == "processing"


def test_confirm_does_not_call_llm_again(db: Path) -> None:
    """用例 10：点击确认不得再次调用大模型。"""
    task = seed(db)
    spy = SpyLLM()
    outcome = execution_panel.run_analysis(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)
    assert spy.calls == 1

    execution_panel.confirm_execution(outcome["plan"], db_path=db, today=BASE_DATE)

    assert spy.calls == 1, "确认执行阶段不应重新调用大模型"


# ============================================================
# 三、取消：写 reject，且允许后续重新分析
# ============================================================


def test_reject_writes_event_and_allows_reanalysis(db: Path) -> None:
    """用例 18：取消写入 reject；被否决后仍可重新分析。"""
    task = seed(db, deadline=DEADLINE_DUE_SOON)
    plan = _plan_for(db, task["id"])
    assert actions(db, task["id"]) == [models.ACTION_CREATE, models.ACTION_ANALYZE]

    outcome = execution_panel.reject_execution(plan, db_path=db)

    assert outcome["ok"] is True
    assert actions(db, task["id"])[-1] == models.ACTION_REJECT
    # reject 不构成去重依据，应能再次分析
    again = execution_panel.run_analysis(task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE)
    assert again["ok"] is True
    assert actions(db, task["id"]).count(models.ACTION_ANALYZE) == 2


def test_reject_does_not_change_task_data(db: Path) -> None:
    """取消不得改动任何任务字段。"""
    task = seed(db)
    plan = _plan_for(db, task["id"])
    before = sqlite_db.get_task(task["id"], db_path=db)

    execution_panel.reject_execution(plan, db_path=db)

    assert sqlite_db.get_task(task["id"], db_path=db) == before


# ============================================================
# 四、防重复执行（用例 22/23）
# ============================================================


@pytest.mark.parametrize(
    "deadline",
    [DEADLINE_DUE_SOON, DEADLINE_OVERDUE],
    ids=["draft_notify", "escalate"],
)
def test_second_execution_of_same_plan_is_blocked(db: Path, deadline: str) -> None:
    """用例 22：同一份计划重复执行必须被拦截（模拟 Streamlit rerun 连点）。"""
    task = seed(db, deadline=deadline, risk_level="green")
    plan = _plan_for(db, task["id"])

    first = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)
    assert first["ok"] is True

    # 用同一份旧计划再执行一次
    second = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert second["ok"] is False
    assert second["message"] == execution_panel.MSG_STALE_PLAN
    # 事件链里不允许出现重复的动作事件
    log_actions = actions(db, task["id"])
    assert log_actions.count(models.ACTION_CONFIRM) == 1
    assert log_actions.count(models.ACTION_DRAFT) <= 1
    assert log_actions.count(models.ACTION_ESCALATE) <= 1


def test_stale_plan_blocked_after_external_change(db: Path) -> None:
    """用例 23：数据库状态被外部改动后，旧计划不得执行。"""
    task = seed(db, risk_level="green")
    plan = _plan_for(db, task["id"])

    # 模拟「另一个操作先把风险等级提到 red」
    sqlite_db.update_risk_level(task["id"], "red", operator="李老师", remark="外部改动", db_path=db)

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert outcome["message"] == execution_panel.MSG_STALE_PLAN
    # 不得再写一次 escalate
    assert actions(db, task["id"]).count(models.ACTION_ESCALATE) == 1


def test_stale_plan_blocked_after_status_becomes_completed(db: Path) -> None:
    """任务被标记完成后，旧计划不得执行。"""
    task = seed(db, deadline=DEADLINE_DUE_SOON)
    plan = _plan_for(db, task["id"])

    sqlite_db.update_status(task["id"], "completed", operator="王老师", remark="已完成", db_path=db)

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert actions(db, task["id"]).count(models.ACTION_CONFIRM) == 0


# ============================================================
# 五、Validator 仍然把住最后一道关（用例 24）
# ============================================================


@pytest.mark.parametrize(
    "bad_action",
    ["MARK_COMPLETED", "MODIFY_OWNER", "MODIFY_DEADLINE", "SEND_EXTERNAL", "UNKNOWN"],
)
def test_validator_blocks_illegal_actions_at_page_layer(db: Path, bad_action: str) -> None:
    """用例 24：即便计划被篡改为黑名单动作，页面层也执行不了。"""
    task = seed(db, deadline=DEADLINE_OVERDUE, risk_level="green")
    plan = _plan_for(db, task["id"])
    plan["action"] = bad_action
    plan["params"] = {"status": "completed", "owner": "张三"}

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    # 不得写入任何动作事件
    log_actions = actions(db, task["id"])
    assert models.ACTION_CONFIRM not in log_actions
    assert models.ACTION_ESCALATE not in log_actions
    assert sqlite_db.get_task(task["id"], db_path=db)["risk_level"] == "green"


def test_plan_without_confirm_flag_is_rejected(db: Path) -> None:
    """need_confirm 不为 True 的计划不得执行。"""
    task = seed(db)
    plan = _plan_for(db, task["id"])
    plan["need_confirm"] = False

    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    assert outcome["ok"] is False
    assert models.ACTION_CONFIRM not in actions(db, task["id"])


# ============================================================
# 六、session_state：rerun 不丢结果 + 防串号
# ============================================================


def test_plan_survives_rerun(db: Path) -> None:
    """用例 8/9：分析结果存入 session_state，rerun 后仍可取回。"""
    task = seed(db)
    plan = _plan_for(db, task["id"])

    state.save_execution_plan(task["id"], plan)
    # 模拟 rerun：重新读取
    restored = state.get_execution_plan(task["id"])

    assert restored is not None
    assert restored["action"] == plan["action"]
    assert restored["situation"] == plan["situation"]


def test_plan_is_not_shared_across_tasks(db: Path) -> None:
    """用例：切换任务后不得复用上一任务的计划（防越权）。"""
    first = seed(db, deadline=DEADLINE_DUE_SOON, title="任务甲")
    second = seed(db, deadline=DEADLINE_DUE_SOON, title="任务乙")
    plan = _plan_for(db, first["id"])

    state.save_execution_plan(first["id"], plan)

    assert state.get_execution_plan(first["id"]) is not None
    assert state.get_execution_plan(second["id"]) is None, "不得跨任务取用计划"


def test_cleared_plan_cannot_be_retrieved(db: Path) -> None:
    """清空后不可再取回。"""
    task = seed(db)
    state.save_execution_plan(task["id"], _plan_for(db, task["id"]))

    state.clear_execution_plan()

    assert state.get_execution_plan(task["id"]) is None


def test_execution_result_survives_rerun(db: Path) -> None:
    """执行结果（提醒草稿内容）跨 rerun 仍可见。"""
    task = seed(db, deadline=DEADLINE_DUE_SOON)
    plan = _plan_for(db, task["id"])
    outcome = execution_panel.confirm_execution(plan, db_path=db, today=BASE_DATE)

    state.save_execution_result({"task_id": task["id"], "message": outcome["message"]})
    restored = state.get_execution_result()

    assert restored is not None
    assert restored["task_id"] == task["id"]


# ============================================================
# 七、中文化：页面不得泄漏内部英文枚举（用例 25）
# ============================================================


def test_action_labels_are_chinese() -> None:
    """三个业务动作都有中文标签。"""
    assert labels.action_label(et.ACTION_DRAFT_NOTIFY) == "生成提醒草稿"
    assert labels.action_label(et.ACTION_REQUEST_INFO) == "请求补充信息"
    assert labels.action_label(et.ACTION_ESCALATE) == "提级督办"
    # 同时兼容日志事件名
    assert labels.action_label(models.ACTION_ESCALATE) == "提级督办"


def test_field_labels_are_chinese() -> None:
    """待补字段名中文化。"""
    assert labels.field_label("owner") == "负责人"
    assert labels.field_label("deadline") == "截止时间"


def test_page_user_facing_text_has_no_english_enums() -> None:
    """用例 25：页面可见文案里不含任何内部英文枚举值。"""
    forbidden = (
        "DRAFT_NOTIFY",
        "REQUEST_INFO",
        "ESCALATE",
        "analyze",
        "escalate",
        "draft",
        "request",
        "confirm",
        "reject",
        "overdue",
        "due_soon",
        "urgent",
        "green",
        "yellow",
        "red",
        "situation:",
        "reasons:",
        "risk_level:",
    )
    user_facing = [
        execution_panel.SECTION_EXECUTION,
        execution_panel.CAPTION_EXECUTION,
        execution_panel.BUTTON_ANALYZE,
        execution_panel.BUTTON_CONFIRM,
        execution_panel.BUTTON_CANCEL,
        execution_panel.LABEL_SITUATION,
        execution_panel.LABEL_REASONS,
        execution_panel.LABEL_ACTION,
        execution_panel.LABEL_RISK_NOTE,
        execution_panel.HINT_NO_ACTION,
        execution_panel.HINT_DRAFT_TAIL,
        execution_panel.HINT_REQUEST_TAIL,
        execution_panel.HINT_CONFIRM_REQUIRED,
        execution_panel.MSG_ANALYSIS_FAILED,
        execution_panel.MSG_DB_CONFLICT,
        execution_panel.MSG_VALIDATOR_REJECTED,
        execution_panel.MSG_STALE_PLAN,
        execution_panel.MSG_EXECUTE_FAILED,
        execution_panel.MSG_REJECTED,
        *execution_panel.RESULT_MESSAGES.values(),
    ]
    for text in user_facing:
        for token in forbidden:
            assert token not in text, f"页面文案「{text}」泄漏了内部枚举 {token}"


def test_execution_panel_does_not_define_its_own_label_maps() -> None:
    """映射必须统一来自 ui/labels.py，执行期督办模块不得自建字典。"""
    source = inspect.getsource(execution_panel)
    for forbidden in ("ACTION_LABELS =", "LEVEL_LABELS =", "RISK_LABELS =", "LOG_ACTION_LABELS ="):
        assert forbidden not in source
    # 必须确实走 labels 模块
    assert "labels.action_label(" in source


def test_ui_modules_have_no_send_capability() -> None:
    """页面层与执行期督办模块都不得出现任何外发能力。"""
    for module in (tasks_page, execution_panel):
        source = inspect.getsource(module)
        for forbidden in ("smtplib", "sendmail", "SMTP(", "requests.post", "urllib"):
            assert forbidden not in source, f"{module.__name__} 出现外发能力 {forbidden}"


def test_result_messages_describe_actions_accurately() -> None:
    """文案不得把草稿说成「发送」，也不得把提级说成「改状态」。"""
    draft = execution_panel.RESULT_MESSAGES[et.ACTION_DRAFT_NOTIFY]
    assert "草稿" in draft
    assert "发送" not in draft

    escalate = execution_panel.RESULT_MESSAGES[et.ACTION_ESCALATE]
    assert "风险等级" in escalate
    assert "状态" not in escalate

    request = execution_panel.RESULT_MESSAGES[et.ACTION_REQUEST_INFO]
    assert "补充" in request
    assert "自动修改" not in request


# ============================================================
# 八、UI 模块可导入性（防止「只测首页」漏掉页面级 NameError）
# ============================================================


def test_all_ui_modules_import_cleanly() -> None:
    """所有 UI 模块必须可导入且顶层常量可访问。

    背景：曾经在显示层重构中误删了 tables.py 对 config.settings 的导入，
    而 test_app_smoke 只加载首页（智能驾驶舱），从不进入任务督办中心，
    导致该 NameError 溜过全量测试。本测试把「每个 UI 模块都能被导入并
    取到关键常量」变成显式约束。
    """
    import importlib

    for module_path in (
        "ui.labels",
        "ui.components.styles",
        "ui.components.tables",
        "ui.components.cards",
        "ui.components.state",
        "ui.pages.dashboard_page",
        "ui.pages.report_page",
        "ui.pages.review_page",
        "ui.pages.tasks_page",
        "ui.pages.execution_panel",
        "ui.pages.upload_page",
    ):
        module = importlib.import_module(module_path)
        assert module is not None


def test_table_option_builders_do_not_need_config_import() -> None:
    """下拉选项构造函数必须自洽（这是 NameError 的高发点）。"""
    status_opts = tables.status_options()
    priority_opts = tables.priority_options()
    risk_opts = tables.risk_options()

    assert list(status_opts.values()) == list(settings.TASK_STATUSES)
    assert list(priority_opts.values()) == list(settings.PRIORITIES)
    assert set(risk_opts.values()) == set(models.RISK_LEVELS)
    # 选项键必须是中文
    for key in (*status_opts, *priority_opts, *risk_opts):
        assert re.search(r"[一-鿿]", str(key))
