"""agent/execution_agent.py 单元测试（v2.0 P0-4）。

测试策略：
1. **一律注入假模型 llm_fn，不调用真实 DeepSeek API**（与 test_task_agent 同策略）；
2. 重点验证**真实业务链的顺序与副作用**，而不是堆砌纯函数断言：
   早退不调模型 → 读历史进 Prompt → 动作来自规则 → 模型输出被严格校验 →
   Validator 拦截 → analyze 在人工确认前落库 → 绝不执行动作；
3. 全部时间相关断言注入固定基准日 BASE_DATE，不依赖系统日期；
4. 核心安全测试（A/B/C 三组）确保大模型无法控制动作、无法越权。

所有任务数据均为演示数据（Synthetic Data），每个测试使用 tmp_path 下的
独立数据库，不触碰真实的 data/campusflow.db。
"""

from __future__ import annotations

import inspect
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent import execution_agent
from database import models, sqlite_db
from llm import client, prompts
from tools import execution_tool as et

# ============================================================
# 固定基准数据
# ============================================================

BASE_DATE = date(2026, 10, 5)

DEADLINE_OVERDUE = "2026-10-03"   # days_left = -2 -> overdue
DEADLINE_URGENT = "2026-10-06"    # days_left = +1 -> urgent
DEADLINE_DUE_SOON = "2026-10-07"  # days_left = +2 -> due_soon
DEADLINE_NORMAL = "2026-10-20"    # days_left = +15 -> normal

GOOD_ANALYSIS: dict[str, Any] = {
    "situation": "任务已逾期 2 天，当前仍处于待办状态，历史中未发现完成记录。",
    "reasons": [
        "截止时间 2026-10-03 已早于当前日期 2026-10-05",
        "任务状态仍为 pending，未进入 processing",
        "历史日志中只有 create 事件，没有任何状态推进记录",
    ],
}


# 哨兵：区分「未显式提供 payload」与「显式传入 None」
_UNSET = object()


class SpyLLM:
    """记录调用次数与 Prompt 内容的假模型。"""

    def __init__(self, payload: Any = _UNSET) -> None:
        self.payload = GOOD_ANALYSIS if payload is _UNSET else payload
        self.calls: list[dict[str, str]] = []

    def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        self.calls.append({"system": system_prompt, "user": user_prompt})
        return self.payload

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def user_prompt(self) -> str:
        return self.calls[-1]["user"]

    @property
    def system_prompt(self) -> str:
        return self.calls[-1]["system"]


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """创建并初始化一个临时数据库。"""
    db_path = tmp_path / "execution_agent_test.db"
    sqlite_db.init_db(db_path)
    return db_path


def seed_task(db_path: Path, **overrides: Any) -> dict[str, Any]:
    """写入一条任务并返回完整记录。"""
    payload: dict[str, Any] = {
        "title": "实验室安全检查",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": DEADLINE_OVERDUE,
        "priority": "high",
        "status": "pending",
        "risk_level": "green",
        "description": "排查实验室安全隐患并建立台账",
    }
    payload.update(overrides)
    created = sqlite_db.create_task(payload, db_path=db_path)
    return sqlite_db.get_task(created["id"], db_path=db_path)  # type: ignore[index]


def log_actions(db_path: Path, task_id: int) -> list[str]:
    """读取某任务的日志动作序列。"""
    return [log["action"] for log in sqlite_db.get_logs(task_id, db_path=db_path)]


# ============================================================
# 一、早退判断：三条路径都必须零调用大模型
# ============================================================


def test_completed_task_returns_none_without_llm(db: Path) -> None:
    """用例 1：已完成任务 -> None，且 LLM 调用次数为 0。"""
    task = seed_task(db, status="completed")
    spy = SpyLLM()

    result = execution_agent.analyze(
        task["id"], llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result is None
    assert spy.call_count == 0


def test_missing_deadline_returns_none_without_llm(db: Path) -> None:
    """用例 2：无截止时间 -> None，且 LLM 调用次数为 0。"""
    task = seed_task(db, deadline=None)
    spy = SpyLLM()

    result = execution_agent.analyze(
        task["id"], llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result is None
    assert spy.call_count == 0


def test_normal_level_returns_none_without_llm(db: Path) -> None:
    """用例 3：提醒等级为 normal -> None，且 LLM 调用次数为 0。"""
    task = seed_task(db, deadline=DEADLINE_NORMAL)
    spy = SpyLLM()

    result = execution_agent.analyze(
        task["id"], llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result is None
    assert spy.call_count == 0


def test_missing_task_raises_project_exception(db: Path) -> None:
    """任务不存在时沿用项目既有异常体系，不吞异常、不返回空结果。"""
    spy = SpyLLM()
    with pytest.raises(sqlite_db.TaskNotFoundError):
        execution_agent.analyze(9999, llm_fn=spy, db_path=db, today=BASE_DATE)
    assert spy.call_count == 0


# ============================================================
# 二、动作由规则决定（用例 4~7）
# ============================================================


def test_overdue_not_red_escalates(db: Path) -> None:
    """用例 4：逾期且非 red -> action=ESCALATE，且确实调用了大模型。"""
    task = seed_task(db, risk_level="green")
    spy = SpyLLM()

    result = execution_agent.analyze(
        task["id"], llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert spy.call_count == 1
    assert result is not None
    assert result["action"] == et.ACTION_ESCALATE
    assert result["params"] == {"risk_level": "red"}
    assert result["ok"] is True


def test_due_soon_drafts_notification(db: Path) -> None:
    """用例 5：临期 -> action=DRAFT_NOTIFY。"""
    task = seed_task(db, deadline=DEADLINE_DUE_SOON)
    spy = SpyLLM()

    result = execution_agent.analyze(
        task["id"], llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["action"] == et.ACTION_DRAFT_NOTIFY
    assert result["params"] == {}


def test_urgent_drafts_notification(db: Path) -> None:
    """用例 5 变体：紧急 -> action=DRAFT_NOTIFY。"""
    task = seed_task(db, deadline=DEADLINE_URGENT)
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert result is not None
    assert result["action"] == et.ACTION_DRAFT_NOTIFY


def test_overdue_already_red_downgrades_to_draft(db: Path) -> None:
    """用例 6：逾期但已 red -> 降级为 DRAFT_NOTIFY，不重复提级。"""
    task = seed_task(db, risk_level="red")
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["action"] == et.ACTION_DRAFT_NOTIFY
    assert result["ok"] is True


def test_missing_owner_requests_info(db: Path) -> None:
    """用例 7：负责人缺失 -> action=REQUEST_INFO。"""
    task = seed_task(db, owner=None)
    spy = SpyLLM()

    result = execution_agent.analyze(
        task["id"], llm_fn=spy, db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["action"] == et.ACTION_REQUEST_INFO
    assert spy.call_count == 1


# ============================================================
# 三、历史事件正确进入 Prompt（用例 8）
# ============================================================


def test_history_enters_prompt(db: Path) -> None:
    """用例 8：历史事件的 action/operator/remark/created_at 必须进入分析上下文。"""
    task = seed_task(db, status="processing")
    sqlite_db.append_log(
        task["id"],
        models.ACTION_ANALYZE,
        operator="AI",
        remark="[ESCALATE] level=overdue days=-2 | 上轮分析",
        db_path=db,
    )
    spy = SpyLLM()

    execution_agent.analyze(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    prompt = spy.user_prompt
    assert "最近历史事件" in prompt
    assert models.ACTION_CREATE in prompt
    assert models.ACTION_ANALYZE in prompt
    assert "上轮分析" in prompt
    # 历史字段白名单里的四类信息都应出现
    for field in execution_agent.HISTORY_CONTEXT_FIELDS:
        assert field in prompt


def test_history_block_is_empty_marker_when_no_logs(db: Path) -> None:
    """无历史时使用明确占位说明，不留空段落。"""
    # create_task 本身会写 create 日志，这里直接测渲染函数
    assert execution_agent._render_history([]) == execution_agent.NO_HISTORY_TEXT
    assert execution_agent._render_history([None, "脏数据"]) == execution_agent.NO_HISTORY_TEXT  # type: ignore[list-item]


def test_history_is_capped_to_limit(db: Path) -> None:
    """历史超过上限时只取最近若干条，避免 Prompt 无限膨胀。"""
    entries = [
        {"action": models.ACTION_ANALYZE, "operator": "AI", "remark": f"第 {i} 条", "created_at": "2026-10-01 09:00:00"}
        for i in range(execution_agent.HISTORY_LIMIT + 8)
    ]
    rendered = execution_agent._render_history(entries)
    parsed = json.loads(rendered)

    assert len(parsed) == execution_agent.HISTORY_LIMIT
    # 保留的是最近的部分
    assert parsed[-1]["remark"] == f"第 {execution_agent.HISTORY_LIMIT + 7} 条"
    assert "第 0 条" not in rendered


def test_task_context_uses_whitelist_only(db: Path) -> None:
    """任务上下文只暴露白名单字段，不整体倾倒 task 记录。"""
    task = seed_task(db, description="排查实验室安全隐患")
    parsed = json.loads(execution_agent._render_task_context(task))

    assert set(parsed) == set(execution_agent.TASK_CONTEXT_FIELDS)
    # 白名单里没有的字段不得出现
    assert "created_at" not in parsed
    assert "need_confirm" not in parsed


def test_prompt_contains_time_and_level(db: Path) -> None:
    """Prompt 必须携带当前时间、提醒等级与剩余天数。"""
    task = seed_task(db)
    spy = SpyLLM()

    execution_agent.analyze(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    prompt = spy.user_prompt
    assert "2026-10-05" in prompt          # 当前时间
    assert "overdue" in prompt             # 系统判定的等级
    assert "-2" in prompt                  # 剩余天数


def test_system_prompt_forbids_action_fields(db: Path) -> None:
    """System Prompt 必须明确禁止模型输出动作与受保护字段。"""
    task = seed_task(db)
    spy = SpyLLM()

    execution_agent.analyze(task["id"], llm_fn=spy, db_path=db, today=BASE_DATE)

    system = spy.system_prompt
    assert system == prompts.EXECUTION_ANALYSIS_SYSTEM
    for forbidden in ("action", "params", "need_confirm", "risk_level", "status"):
        assert forbidden in system
    assert "situation" in system
    assert "reasons" in system


# ============================================================
# 四、完整计划返回（用例 9）
# ============================================================


def test_returns_complete_plan(db: Path) -> None:
    """用例 9：正常返回必须包含 UI 所需的全部字段。"""
    task = seed_task(db)
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert result is not None
    for key in (
        "ok",
        "task_id",
        "action",
        "params",
        "need_confirm",
        "level",
        "days_left",
        "situation",
        "reasons",
        "risk_note",
        "plan",
        "error",
    ):
        assert key in result, f"缺少字段 {key}"

    assert result["task_id"] == task["id"]
    assert result["need_confirm"] is True
    assert result["situation"] == GOOD_ANALYSIS["situation"]
    assert result["reasons"] == GOOD_ANALYSIS["reasons"]
    assert result["days_left"] == -2
    assert result["error"] is None


def test_plan_contains_days_left(db: Path) -> None:
    """规范化计划里必须带上剩余天数。"""
    task = seed_task(db)
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert result is not None
    assert result["plan"]["days_left"] == -2


def test_risk_note_is_rule_generated(db: Path) -> None:
    """风险说明必须由规则生成，且与动作一致。"""
    overdue_task = seed_task(db)
    result = execution_agent.analyze(
        overdue_task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert result is not None
    assert "red" in result["risk_note"]
    assert "不会修改任务状态" in result["risk_note"]

    due_task = seed_task(db, deadline=DEADLINE_DUE_SOON)
    result2 = execution_agent.analyze(
        due_task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert result2 is not None
    assert "不会自动发送" in result2["risk_note"]


def test_build_risk_note_covers_all_actions() -> None:
    """三个动作都必须有明确的风险说明，不允许落到空文案。"""
    for action in et.ALLOWED_ACTIONS:
        note = execution_agent.build_risk_note(action, "overdue", "-2")
        assert note
        assert note != execution_agent.build_risk_note("", "", "")
    assert execution_agent.build_risk_note("", "", "") == "当前无需处置。"


# ============================================================
# 五、LLM 输出严格校验（用例 10~13）
# ============================================================


def test_illegal_json_raises_and_writes_nothing(db: Path) -> None:
    """用例 10：模型返回非法 JSON -> 失败，且不写任何日志、不改任务。"""
    task = seed_task(db)
    before = sqlite_db.get_task(task["id"], db_path=db)

    def broken_llm(system_prompt: str, user_prompt: str) -> Any:
        raise client.LLMError("模型返回内容不是合法 JSON")

    with pytest.raises(execution_agent.ExecutionAnalysisError):
        execution_agent.analyze(task["id"], llm_fn=broken_llm, db_path=db, today=BASE_DATE)

    assert log_actions(db, task["id"]) == [models.ACTION_CREATE]
    assert sqlite_db.get_task(task["id"], db_path=db) == before


def test_llm_error_is_wrapped_but_chain_preserved(db: Path) -> None:
    """原始 LLMError 不丢失：包装后仍可通过 __cause__ 追溯。"""
    task = seed_task(db)

    def broken_llm(system_prompt: str, user_prompt: str) -> Any:
        raise client.LLMError("模拟限流")

    with pytest.raises(execution_agent.ExecutionAnalysisError) as info:
        execution_agent.analyze(task["id"], llm_fn=broken_llm, db_path=db, today=BASE_DATE)

    assert isinstance(info.value.__cause__, client.LLMError)
    assert "模拟限流" in str(info.value)


def test_malformed_structure_raises_execution_analysis_error(db: Path) -> None:
    """模型返回非 JSON 可解析结构时也必须失败（含显式 None）。"""
    task = seed_task(db)
    for payload in (["a", "list"], "纯字符串", 42, None, True):
        with pytest.raises(execution_agent.ExecutionAnalysisError):
            execution_agent.analyze(
                task["id"], llm_fn=SpyLLM(payload), db_path=db, today=BASE_DATE
            )
    assert log_actions(db, task["id"]) == [models.ACTION_CREATE]


def test_empty_situation_raises(db: Path) -> None:
    """用例 11：situation 为空或非字符串 -> 失败。"""
    task = seed_task(db)
    for bad in ("", "   ", None, 123):
        payload = {"situation": bad, "reasons": ["依据"]}
        with pytest.raises(execution_agent.ExecutionAnalysisError):
            execution_agent.analyze(
                task["id"], llm_fn=SpyLLM(payload), db_path=db, today=BASE_DATE
            )
    assert log_actions(db, task["id"]) == [models.ACTION_CREATE]


def test_empty_reasons_raises(db: Path) -> None:
    """用例 12：reasons 为空列表或全空白 -> 失败，绝不伪造依据。"""
    task = seed_task(db)
    for bad in ([], ["", "   "], None, "不是列表"):
        payload = {"situation": "有情况", "reasons": bad}
        with pytest.raises(execution_agent.ExecutionAnalysisError):
            execution_agent.analyze(
                task["id"], llm_fn=SpyLLM(payload), db_path=db, today=BASE_DATE
            )
    assert log_actions(db, task["id"]) == [models.ACTION_CREATE]


def test_non_string_reason_raises(db: Path) -> None:
    """用例 13：reasons 中混入非字符串 -> 失败（不做静默过滤）。"""
    task = seed_task(db)
    for bad in ([1, 2], ["有效依据", None], [{"text": "x"}], [True]):
        payload = {"situation": "有情况", "reasons": bad}
        with pytest.raises(execution_agent.ExecutionAnalysisError):
            execution_agent.analyze(
                task["id"], llm_fn=SpyLLM(payload), db_path=db, today=BASE_DATE
            )
    assert log_actions(db, task["id"]) == [models.ACTION_CREATE]


def test_reasons_are_cleaned_and_deduped(db: Path) -> None:
    """有效 reasons 会被去空白与去重，顺序保持不变。"""
    payload = {
        "situation": "  情况说明  ",
        "reasons": ["  依据一  ", "依据一", "依据二", "   "],
    }
    situation, reasons = execution_agent._parse_analysis(payload)
    assert situation == "情况说明"
    assert reasons == ["依据一", "依据二"]


def test_no_fake_content_on_llm_failure(db: Path) -> None:
    """核心原则：LLM 失败时绝不生成假的 situation/reasons。"""
    task = seed_task(db)

    def failing_llm(system_prompt: str, user_prompt: str) -> Any:
        raise client.LLMError("模拟超时")

    with pytest.raises(execution_agent.ExecutionAnalysisError):
        execution_agent.analyze(
            task["id"], llm_fn=failing_llm, db_path=db, today=BASE_DATE
        )

    # 规则层仍能独立判定动作，但不得写库、不得返回假结果
    assert log_actions(db, task["id"]) == [models.ACTION_CREATE]
    assert sqlite_db.get_task(task["id"], db_path=db)["risk_level"] == "green"


# ============================================================
# 六、核心安全测试：LLM 无法控制动作
# ============================================================


def test_security_a_llm_cannot_inject_action_mark_completed(db: Path) -> None:
    """安全 A：模型返回的动作被完全丢弃，plan.action 仍来自 select_action。"""
    task = seed_task(db, deadline=DEADLINE_DUE_SOON)
    hostile = {
        "action": "MARK_COMPLETED",
        "params": {"status": "completed"},
        "situation": "看起来已经做完了",
        "reasons": ["模型主观判断已完成"],
    }
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(hostile), db_path=db, today=BASE_DATE
    )

    assert result is not None
    # 规则判定为 DRAFT_NOTIFY，绝不是模型声称的完成动作
    assert result["action"] == et.ACTION_DRAFT_NOTIFY
    assert result["plan"]["action"] == et.ACTION_DRAFT_NOTIFY
    assert result["params"] == {}
    # 任务状态未被模型影响
    assert sqlite_db.get_task(task["id"], db_path=db)["status"] == "pending"


@pytest.mark.parametrize(
    "hostile",
    [
        {"action": "MODIFY_OWNER", "params": {"owner": "张三"}},
        {"action": "MODIFY_DEADLINE", "params": {"deadline": "2099-01-01"}},
        {"action": "MODIFY_TITLE", "params": {"title": "改标题"}},
        {"action": "MODIFY_STATUS", "params": {"status": "urgent"}},
        {"action": "SEND_EXTERNAL", "params": {"channel": "sms"}},
    ],
)
def test_security_b_llm_modify_proposals_are_ignored(db: Path, hostile: dict[str, Any]) -> None:
    """安全 B：模型试图改受保护字段时，动作与参数一律被规则覆盖。"""
    task = seed_task(db, deadline=DEADLINE_DUE_SOON)
    payload = {**hostile, "situation": "模型想直接改", "reasons": ["模型声称"]}

    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(payload), db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["action"] == et.ACTION_DRAFT_NOTIFY
    assert result["params"] == {}
    # plan 中不得残留任何受保护字段
    assert set(result["plan"]["params"]) & set(et.FORBIDDEN_PARAM_FIELDS) == set()
    # 任务数据完全未被模型改动
    after = sqlite_db.get_task(task["id"], db_path=db)
    assert after["owner"] == "王老师"
    assert after["deadline"] == DEADLINE_DUE_SOON
    assert after["title"] == "实验室安全检查"


def test_security_c_execution_agent_source_has_no_privileged_operations() -> None:
    """安全 C：结构自证——本模块源码不含任何越权能力。

    这是防止后续开发把越权能力误加回来的结构性测试，
    与 tests/test_report_agent.py 中 inspect.getsource 的既有范式一致。
    """
    source = inspect.getsource(execution_agent)
    for forbidden in (
        "update_owner",
        "update_deadline",
        "update_status",
        "MARK_COMPLETED",
        "SEND_EXTERNAL",
        "smtplib",
        "sendmail",
    ):
        assert forbidden not in source, f"execution_agent 不应出现 {forbidden}"


def test_execution_agent_writes_only_via_sqlite_db_public_api() -> None:
    """数据库写入必须走 sqlite_db 公开接口，不得自建连接或裸写 SQL。"""
    source = inspect.getsource(execution_agent)

    # 唯一允许的写入入口
    assert "sqlite_db.append_log" in source

    # 不得自行 import sqlite3（而非"文中提到 sqlite3"）
    assert "import sqlite3" not in source
    assert not hasattr(execution_agent, "sqlite3")

    # 不得直接执行 SQL 语句
    for sql in ("UPDATE tasks", "INSERT INTO", "DELETE FROM", "PRAGMA"):
        assert sql not in source


# ============================================================
# 七、Validator 联动（用例 16~18）
# ============================================================


def test_plan_passes_validator(db: Path) -> None:
    """用例 16：产出的计划必须能通过 Validator。"""
    task = seed_task(db)
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert result is not None
    assert result["ok"] is True
    assert result["error"] is None

    # 用同一套数据独立复验一次，确认确实是 Validator 认可的
    fresh = sqlite_db.get_task(task["id"], db_path=db)
    logs = sqlite_db.get_logs(task["id"], db_path=db)
    validation = et.validate_plan(fresh, result["plan"], logs, today=BASE_DATE)
    assert validation.ok is True


def test_validator_rejection_writes_no_analyze(db: Path) -> None:
    """用例 17：Validator 拒绝时不写 analyze、不改任务、返回明确原因。

    构造方式：先造一条近期 draft 日志，触发 Validator 的 L6 去重拦截。
    """
    task = seed_task(db, deadline=DEADLINE_DUE_SOON, risk_level="green")
    sqlite_db.append_log(
        task["id"],
        models.ACTION_DRAFT,
        operator="张老师",
        remark="[DRAFT_NOTIFY] level=due_soon days=2 | 上次已提醒",
        db_path=db,
    )
    before = sqlite_db.get_task(task["id"], db_path=db)

    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["ok"] is False
    assert result["plan"] is None
    assert "不重复处置" in str(result["error"])
    # 未写入 analyze
    assert log_actions(db, task["id"]) == [
        models.ACTION_CREATE,
        models.ACTION_DRAFT,
    ]
    assert sqlite_db.get_task(task["id"], db_path=db) == before


def test_validator_pass_writes_analyze(db: Path) -> None:
    """用例 18：Validator 通过 -> 写入 analyze 事件。"""
    task = seed_task(db)
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["ok"] is True
    assert log_actions(db, task["id"]) == [models.ACTION_CREATE, models.ACTION_ANALYZE]


# ============================================================
# 八、事件链时序（用例 19~20）
# ============================================================


def test_analyze_written_before_human_confirmation(db: Path) -> None:
    """用例 19：analyze 必须在人工确认之前落库。

    证明方式：analyze() 返回后、日志中已出现 analyze，
    但 confirm / escalate 都不存在——因为确认流程尚未发生。
    """
    task = seed_task(db)
    execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    actions = log_actions(db, task["id"])
    assert models.ACTION_ANALYZE in actions
    assert models.ACTION_CONFIRM not in actions
    assert models.ACTION_ESCALATE not in actions
    # 顺序上 analyze 紧随 create，说明事件链起点正确
    assert actions.index(models.ACTION_ANALYZE) > actions.index(models.ACTION_CREATE)


def test_analyze_remark_contains_level_days_and_situation(db: Path) -> None:
    """用例 20：analyze 日志必须可审计——含动作、等级、天数与情况说明。"""
    task = seed_task(db)
    execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    logs = sqlite_db.get_logs(task["id"], db_path=db)
    latest = logs[-1]
    assert latest["action"] == models.ACTION_ANALYZE
    assert et.ACTION_ESCALATE in latest["remark"]
    assert "level=overdue" in latest["remark"]
    assert "days=-2" in latest["remark"]
    assert GOOD_ANALYSIS["situation"] in latest["remark"]
    assert len(latest["remark"]) <= et.REMARK_MAX_LENGTH


def test_append_log_does_not_touch_task_row(db: Path) -> None:
    """事件写入不得改动 tasks 表任何字段（含 updated_at）。"""
    task = seed_task(db)
    before = sqlite_db.get_task(task["id"], db_path=db)
    execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    after = sqlite_db.get_task(task["id"], db_path=db)
    assert after == before


def test_append_log_rejects_unknown_action(db: Path) -> None:
    """append_log 只接受 models.LOG_ACTIONS 内的动作。"""
    task = seed_task(db)
    with pytest.raises(ValueError):
        sqlite_db.append_log(task["id"], "随手写点什么", db_path=db)


def test_append_log_missing_task_raises(db: Path) -> None:
    """append_log 对不存在的任务报错，不留孤儿子键日志。"""
    with pytest.raises(sqlite_db.TaskNotFoundError):
        sqlite_db.append_log(9999, models.ACTION_ANALYZE, db_path=db)


# ============================================================
# 九、重复处置联动（用例 21~22）
# ============================================================


def test_recent_draft_blocks_second_analysis(db: Path) -> None:
    """用例 21：历史已有 draft -> Validator 拒绝重复动作。"""
    task = seed_task(db, deadline=DEADLINE_DUE_SOON)
    sqlite_db.append_log(
        task["id"],
        models.ACTION_DRAFT,
        operator="张老师",
        remark="[DRAFT_NOTIFY] level=due_soon days=2 | 已提醒",
        db_path=db,
    )

    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["ok"] is False
    assert "不重复处置" in str(result["error"])


def test_analyze_then_reject_allows_new_analysis(db: Path) -> None:
    """用例 22：analyze -> reject 之后允许重新分析（不得把任务锁死）。"""
    task = seed_task(db, deadline=DEADLINE_DUE_SOON)
    sqlite_db.append_log(
        task["id"], models.ACTION_ANALYZE, operator="AI", remark="[DRAFT_NOTIFY] level=due_soon days=2 | 首次分析", db_path=db
    )
    sqlite_db.append_log(
        task["id"], models.ACTION_REJECT, operator="张老师", remark="人工否决本次建议", db_path=db
    )

    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["ok"] is True
    assert log_actions(db, task["id"])[-1] == models.ACTION_ANALYZE
    assert log_actions(db, task["id"]).count(models.ACTION_ANALYZE) == 2


# ============================================================
# 十、绝不执行动作（用例 23~24）
# ============================================================


def test_analyze_does_not_execute_escalate(db: Path) -> None:
    """用例 23：analyze 阶段绝不真正执行 ESCALATE。"""
    task = seed_task(db, risk_level="green")
    result = execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )

    assert result is not None
    assert result["action"] == et.ACTION_ESCALATE
    # 只写了 analyze，没有 escalate
    assert log_actions(db, task["id"]) == [models.ACTION_CREATE, models.ACTION_ANALYZE]
    assert models.ACTION_ESCALATE not in log_actions(db, task["id"])


def test_analyze_does_not_change_risk_level(db: Path) -> None:
    """用例 24：analyze 期间不得修改 tasks.risk_level。"""
    task = seed_task(db, risk_level="green")
    execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    assert sqlite_db.get_task(task["id"], db_path=db)["risk_level"] == "green"


def test_analyze_does_not_change_status(db: Path) -> None:
    """analyze 期间不得修改任务状态。"""
    task = seed_task(db, status="processing")
    execution_agent.analyze(
        task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
    )
    after = sqlite_db.get_task(task["id"], db_path=db)
    assert after["status"] == "processing"
    assert after["risk_level"] == "green"


# ============================================================
# 十一、异常路径不得返回假成功（用例 25）
# ============================================================


def test_get_logs_failure_does_not_return_fake_success(
    monkeypatch: pytest.MonkeyPatch, db: Path
) -> None:
    """用例 25：读取历史失败时必须抛出，绝不返回「看起来成功」的结果。"""
    task = seed_task(db)

    def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise sqlite_db.DatabaseError("模拟读取日志失败")

    monkeypatch.setattr(sqlite_db, "get_logs", boom)

    with pytest.raises(sqlite_db.DatabaseError):
        execution_agent.analyze(
            task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
        )


def test_get_task_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, db: Path
) -> None:
    """读取任务失败时同样向上抛出，不静默返回 None。"""
    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise sqlite_db.DatabaseError("模拟读取任务失败")

    monkeypatch.setattr(sqlite_db, "get_task", boom)

    with pytest.raises(sqlite_db.DatabaseError):
        execution_agent.analyze(
            1, llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
        )


def test_append_log_failure_propagates_after_validation(
    monkeypatch: pytest.MonkeyPatch, db: Path
) -> None:
    """analyze 写入失败时向上抛出，绝不返回 ok=True 的假结果。"""
    task = seed_task(db)

    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise sqlite_db.DatabaseError("模拟写入 analyze 失败")

    monkeypatch.setattr(sqlite_db, "append_log", boom)

    with pytest.raises(sqlite_db.DatabaseError):
        execution_agent.analyze(
            task["id"], llm_fn=SpyLLM(), db_path=db, today=BASE_DATE
        )


# ============================================================
# 十二、默认注入路径
# ============================================================


def test_defaults_to_real_llm_client(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    """不注入 llm_fn 时使用 client.chat_json（此处替换为 Spy 以避免真实调用）。"""
    task = seed_task(db)
    spy = SpyLLM()
    calls: list[tuple[str, str]] = []

    def fake_chat_json(system_prompt: str, user_prompt: str, **kwargs: Any) -> Any:
        calls.append((system_prompt, user_prompt))
        return spy(system_prompt, user_prompt)

    monkeypatch.setattr(client, "chat_json", fake_chat_json)

    result = execution_agent.analyze(task["id"], db_path=db, today=BASE_DATE)

    assert result is not None
    assert result["ok"] is True
    assert len(calls) == 1


def test_today_defaults_to_system_date(db: Path) -> None:
    """不注入 today 时使用系统日期；远期截止日因此稳定落在 normal（早退）。"""
    task = seed_task(db, deadline="2099-12-31")
    spy = SpyLLM()
    result = execution_agent.analyze(task["id"], llm_fn=spy, db_path=db)
    assert result is None
    assert spy.call_count == 0


def test_history_limit_constant_is_twenty() -> None:
    """历史条数上限固定为 20。"""
    assert execution_agent.HISTORY_LIMIT == 20
