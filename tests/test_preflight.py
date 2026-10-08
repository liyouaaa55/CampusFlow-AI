"""就绪自检模块（tools/preflight.py）的测试。

核心原则：**断言真实的副作用，而不是函数返回值**。
- 跑一次 check_pipeline 后，直接打开它留下的临时库文件，断言：
  任务真的写进去了、执行后 risk_level 真的变成 red、日志真的多出
  confirm / escalate 记录、上传文件真的落盘。
这样即使将来有人把 preflight 改成「只走形式不落库」，测试也会立刻失败。

打桩说明：LLM 在此用打桩函数是合理的——单元测试不应依赖网络与密钥；
真实模型连通性由 `docs/dev/系统自检.py --deep` 在赛前验证。
"""

from __future__ import annotations

from datetime import date

import pytest

from config import settings
from database import models, sqlite_db
from tools import preflight


# ============================================================
# 一、快速模式总入口
# ============================================================


def test_run_all_returns_report_with_all_categories() -> None:
    """快速自检必须覆盖六个类别，且不抛异常。"""
    report = preflight.run_all(deep=False)
    assert isinstance(report, preflight.CheckReport)
    covered = {item.category for item in report.items}
    assert covered == set(preflight.CATEGORIES)


def test_run_all_does_not_touch_production_database() -> None:
    """自检绝不能污染正式任务库（这是它可以放进应用启动的前提）。"""
    before = len(sqlite_db.list_tasks())
    preflight.run_all(deep=False)
    after = len(sqlite_db.list_tasks())
    assert before == after


def test_quick_mode_reports_api_connectivity_as_warning() -> None:
    """快速模式不发网络请求，连通性必须是「提醒」而不是假装「通过」。"""
    items = preflight.check_llm(deep=False)
    connectivity = next(item for item in items if item.name == "API 连通性")
    assert connectivity.status == preflight.WARN


def test_missing_key_is_reported_as_failure() -> None:
    """没有密钥时必须判 FAIL（否则等于没检查）。"""
    items = preflight.check_llm(deep=False)
    key_item = next(item for item in items if item.name == "密钥配置")
    assert key_item.status == (preflight.PASS if settings.is_llm_ready() else preflight.FAIL)


def test_pdf_check_is_never_blocking() -> None:
    """PDF 无法自动造样本，必须是 WARN 而非 FAIL（不谎报阻断）。"""
    items = preflight.check_parsers()
    pdf = next(item for item in items if ".pdf" in item.name)
    assert pdf.status in (preflight.PASS, preflight.WARN)


def test_parsers_detect_all_supported_formats() -> None:
    """五种可自动验证的格式必须全部真实解析成功（不是只检查库是否安装）。"""
    items = preflight.check_parsers()
    statuses = {item.name: item.status for item in items}
    for key in (".docx", ".xlsx", ".csv", ".txt", ".md"):
        matched = [name for name in statuses if key in name]
        assert matched, f"缺少 {key} 的检查项"
        assert all(statuses[name] == preflight.PASS for name in matched), f"{key} 解析未通过"


def test_agents_entrypoints_all_available() -> None:
    """五个 Agent 的公开入口必须都存在。"""
    items = preflight.check_agents()
    agent_items = [item for item in items if item.name.startswith(tuple(
        ("TaskAgent", "PlannerAgent", "ReminderAgent", "ReportAgent", "ExecutionAgent")
    ))]
    assert len(agent_items) == len(preflight.AGENT_ENTRYPOINTS)
    assert all(item.status == preflight.PASS for item in agent_items)


# ============================================================
# 二、端到端链路：断言数据库与日志真实变化
# ============================================================


@pytest.fixture()
def chain_result(tmp_path):
    """跑一次链路自检，保留工作目录供后续断言。"""
    items = preflight.check_pipeline(llm_fn=preflight._dispatch_stub, workdir=tmp_path)
    databases = sorted(tmp_path.glob("*.db"))
    return items, databases, tmp_path


def test_pipeline_runs_all_nine_steps(chain_result) -> None:
    """九段链路每一步都必须通过，不能有跳步。"""
    items, _databases, _root = chain_result
    names = [item.name for item in items]
    assert len(names) == len(preflight.CHAIN_STEPS)
    for step in preflight.CHAIN_STEPS:
        assert any(step in name for name in names), f"缺少环节：{step}"
    failures = [item.name for item in items if item.status == preflight.FAIL]
    assert not failures, f"链路存在失败环节：{failures}"


def test_pipeline_really_writes_database(chain_result) -> None:
    """核心断言：链路真的把任务写进数据库文件，而不只是返回了结果。"""
    _items, databases, _root = chain_result
    assert databases, "链路没有生成数据库文件"

    tasks = sqlite_db.list_tasks(db_path=databases[0])
    # 至少两条：识别入库的 1 条 + 为触发智能分析专门造的逾期任务 1 条
    assert len(tasks) >= 2

    overdue = [task for task in tasks if "逾期任务" in str(task.get("title"))]
    assert overdue, "未找到用于触发智能分析的逾期任务"


def test_pipeline_really_changes_risk_level(chain_result) -> None:
    """执行 escalate 后，数据库里的 risk_level 必须真的变成 high/red。"""
    _items, databases, _root = chain_result
    tasks = sqlite_db.list_tasks(db_path=databases[0])
    overdue = next(task for task in tasks if "逾期任务" in str(task.get("title")))
    # 造出来时是 high，execute(ESCALATE) 后应升为 red
    assert overdue["risk_level"] == preflight.execution_tool.ESCALATE_TARGET_RISK


def test_pipeline_really_appends_confirm_log(chain_result) -> None:
    """执行后日志必须真的增加，并含人工确认与动作事件。"""
    _items, databases, _root = chain_result
    tasks = sqlite_db.list_tasks(db_path=databases[0])
    overdue = next(task for task in tasks if "逾期任务" in str(task.get("title")))

    logs = sqlite_db.get_logs(overdue["id"], db_path=databases[0])
    actions = [row.get("action") for row in logs]
    assert models.ACTION_CONFIRM in actions, "缺少人工确认留痕"
    assert models.ACTION_CREATE in actions, "缺少创建留痕"
    # 至少 4 条：create + analyze + confirm + escalate
    assert len(logs) >= 4


def test_pipeline_really_writes_upload_file(chain_result) -> None:
    """上传环节必须真的落盘文件（复刻上传页的落盘行为）。"""
    _items, _databases, root = chain_result
    uploads = list((root / "uploads").glob("*"))
    assert uploads, "上传目录为空，落盘环节未真实执行"


def test_pipeline_dashboard_reads_fresh_data(chain_result) -> None:
    """驾驶舱读到的条数必须与库中实际条数一致（证明报表不是旧缓存）。"""
    _items, databases, _root = chain_result
    from agent import report_agent

    tasks = sqlite_db.list_tasks(db_path=databases[0])
    dashboard = report_agent.generate_dashboard(
        today=date.today(), db_path=databases[0]
    )
    assert dashboard["total"] == len(tasks)
