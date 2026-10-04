"""ReportAgent 单元测试。

测试策略：
- 使用 tmp_path 下的独立数据库预置演示数据（Synthetic Data），统计链路真实走
  list_tasks → count_levels → generate_messages；
- 基准日期固定 TODAY=2026-09-23，生成时间固定 GENERATED_AT，保证报告内容可断言。
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from agent import reminder_agent, report_agent
from config import settings
from database import sqlite_db
from tools import report_tool

# 固定基准日期与生成时间
TODAY = date(2026, 9, 23)
GENERATED_AT = datetime(2026, 9, 23, 22, 30, 0)

# 主场景数据：(标题, 部门, 负责人, 相对天数, 状态, 是否待确认)
SEED: list[tuple[str, str | None, str | None, int, str, bool]] = [
    ("宿舍消防隐患专项排查", "保卫处", "王老师", -1, "processing", True),   # 逾期
    ("举办机器人创新周", "学工办", "张老师", 1, "processing", False),        # 紧急
    ("报送教学质量报告", "教务处", None, 2, "pending", True),               # 临期 + 无负责人
    ("更新宣传栏", "学工办", "张老师", 4, "completed", False),              # 已完成
    ("整理活动总结材料", "学工办", "张老师", 6, "completed", False),        # 已完成
    ("清理临时堆放杂物", None, None, 3, "pending", False),                 # 临期 + 无部门
]

# 健康场景：完成率 80%，无逾期、无紧急、无临期
HEALTHY_SEED: list[tuple[str, str | None, str | None, int, str, bool]] = [
    ("完成材料归档", "学工办", "张老师", 2, "completed", False),
    ("汇总季度数据", "学工办", "张老师", 4, "completed", False),
    ("印发会议纪要", "学工办", "张老师", 6, "completed", False),
    ("更新网站内容", "学工办", "张老师", 8, "completed", False),
    ("整理档案目录", "学工办", "张老师", 10, "processing", False),
]

# 全部完成场景
COMPLETE_SEED: list[tuple[str, str | None, str | None, int, str, bool]] = [
    ("任务甲", "学工办", "张老师", -5, "completed", False),
    ("任务乙", "保卫处", "王老师", -1, "completed", False),
]

# 紧急事项聚集场景（urgent = 4 > 3）
URGENT_SEED: list[tuple[str, str | None, str | None, int, str, bool]] = [
    ("紧急任务一", "学工办", "张老师", 1, "processing", False),
    ("紧急任务二", "学工办", "张老师", 1, "processing", False),
    ("紧急任务三", "保卫处", "王老师", 0, "processing", False),
    ("紧急任务四", "保卫处", "王老师", 1, "pending", False),
    ("已完成任务", "学工办", "张老师", -2, "completed", False),
]


def seed(db_path: Path, entries: list[tuple[str, str | None, str | None, int, str, bool]]) -> None:
    """把演示数据写入数据库。"""
    sqlite_db.batch_create(
        [
            {
                "title": title,
                "department": department,
                "owner": owner,
                "deadline": (TODAY + timedelta(days=offset)).isoformat(),
                "priority": "medium",
                "status": status,
                "risk_level": "green",
                "need_confirm": need_confirm,
                "description": None,
            }
            for title, department, owner, offset, status, need_confirm in entries
        ],
        db_path=db_path,
    )


@pytest.fixture()
def weekly_db(tmp_path: Path) -> Path:
    """主场景数据库（6 条任务，覆盖四个级别与信息缺失）。"""
    db_path = sqlite_db.init_db(tmp_path / "weekly.db")
    seed(db_path, SEED)
    return db_path


@pytest.fixture()
def empty_db(tmp_path: Path) -> Path:
    """空数据库。"""
    return sqlite_db.init_db(tmp_path / "empty.db")


@pytest.fixture()
def healthy_db(tmp_path: Path) -> Path:
    """健康数据库（完成率 80%）。"""
    db_path = sqlite_db.init_db(tmp_path / "healthy.db")
    seed(db_path, HEALTHY_SEED)
    return db_path


@pytest.fixture()
def complete_db(tmp_path: Path) -> Path:
    """全部完成（完成率 100%）。"""
    db_path = sqlite_db.init_db(tmp_path / "complete.db")
    seed(db_path, COMPLETE_SEED)
    return db_path


@pytest.fixture()
def urgent_db(tmp_path: Path) -> Path:
    """紧急事项聚集（4 项紧急）。"""
    db_path = sqlite_db.init_db(tmp_path / "urgent.db")
    seed(db_path, URGENT_SEED)
    return db_path


def weekly_report(db_path: Path) -> str:
    """用固定基准日期与生成时间渲染周报，便于断言。"""
    return report_agent.generate_weekly_report(today=TODAY, db_path=db_path, generated_at=GENERATED_AT)


# ============================================================
# 一、generate_dashboard
# ============================================================


def test_dashboard_empty_database(empty_db: Path) -> None:
    """空数据库：全部为 0，部门列表为空。"""
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=empty_db)
    assert dashboard == {
        "total": 0,
        "completed": 0,
        "processing": 0,
        "completion_rate": 0.0,
        "normal": 0,
        "due_soon": 0,
        "urgent": 0,
        "overdue": 0,
        "top_departments": [],
    }


def test_dashboard_field_order(weekly_db: Path) -> None:
    """字段与顺序必须与需求文档一致。"""
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=weekly_db)
    assert list(dashboard.keys()) == list(report_agent.DASHBOARD_FIELDS)


def test_dashboard_counts(weekly_db: Path) -> None:
    """总数 / 已完成 / 进行中统计正确。"""
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=weekly_db)
    assert dashboard["total"] == 6
    assert dashboard["completed"] == 2
    assert dashboard["processing"] == 2


def test_dashboard_completion_rate(weekly_db: Path) -> None:
    """完成率 = 已完成 / 总数，保留一位小数。"""
    assert report_agent.generate_dashboard(today=TODAY, db_path=weekly_db)["completion_rate"] == 33.3


def test_dashboard_completion_rate_full(complete_db: Path) -> None:
    """全部完成时完成率为 100.0。"""
    assert report_agent.generate_dashboard(today=TODAY, db_path=complete_db)["completion_rate"] == 100.0


def test_dashboard_levels_match_count_levels(weekly_db: Path) -> None:
    """四个级别数字直接来自 count_levels()，不重新计算。"""
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=weekly_db)
    levels = reminder_agent.count_levels(today=TODAY, db_path=weekly_db)

    assert (dashboard["normal"], dashboard["due_soon"], dashboard["urgent"], dashboard["overdue"]) == (
        levels["normal"],
        levels["due_soon"],
        levels["urgent"],
        levels["overdue"],
    )
    assert dashboard["overdue"] == 1
    assert dashboard["urgent"] == 1
    assert dashboard["due_soon"] == 2


def test_dashboard_top_departments(weekly_db: Path) -> None:
    """部门排行按任务量降序，未指定部门的任务不计入。"""
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=weekly_db)
    assert dashboard["top_departments"] == [
        {"department": "学工办", "count": 3},
        {"department": "保卫处", "count": 1},
        {"department": "教务处", "count": 1},
    ]


def test_dashboard_top_department_item_keys(weekly_db: Path) -> None:
    """部门条目只包含 department 与 count 两个字段。"""
    item = report_agent.generate_dashboard(today=TODAY, db_path=weekly_db)["top_departments"][0]
    assert list(item.keys()) == ["department", "count"]


@pytest.mark.parametrize(("top_n", "expected_len"), [(1, 1), (2, 2), (3, 3), (0, 0), (10, 3)])
def test_dashboard_top_n(weekly_db: Path, top_n: int, expected_len: int) -> None:
    """top_n 可配置，且不会返回超过实际部门数量的条目。"""
    dashboard = report_agent.generate_dashboard(top_n=top_n, today=TODAY, db_path=weekly_db)
    assert len(dashboard["top_departments"]) == expected_len


def test_dashboard_types(weekly_db: Path) -> None:
    """数值类型：计数为 int，完成率为 float。"""
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=weekly_db)
    assert isinstance(dashboard["total"], int)
    assert isinstance(dashboard["completed"], int)
    assert isinstance(dashboard["completion_rate"], float)
    assert isinstance(dashboard["top_departments"], list)


def test_dashboard_empty_top_departments(empty_db: Path) -> None:
    """空库的部门排行为空列表。"""
    assert report_agent.generate_dashboard(today=TODAY, db_path=empty_db)["top_departments"] == []


# ============================================================
# 二、部门统计
# ============================================================


def test_department_stats_rows(weekly_db: Path) -> None:
    """部门统计：每个部门一行，字段固定。"""
    tasks = sqlite_db.list_tasks(db_path=weekly_db)
    rows = report_agent.department_stats(tasks)

    assert [row["department"] for row in rows] == ["学工办", "保卫处", "教务处"]
    assert list(rows[0].keys()) == ["department", "total", "completed", "completion_rate"]


def test_department_stats_values(weekly_db: Path) -> None:
    """部门完成率计算正确。"""
    rows = {row["department"]: row for row in report_agent.department_stats(sqlite_db.list_tasks(db_path=weekly_db))}
    assert rows["学工办"]["total"] == 3
    assert rows["学工办"]["completed"] == 2
    assert rows["学工办"]["completion_rate"] == 66.7
    assert rows["保卫处"]["completion_rate"] == 0.0


def test_department_stats_sorted(weekly_db: Path) -> None:
    """排序：总数降序 → 部门名称升序。"""
    rows = report_agent.department_stats(sqlite_db.list_tasks(db_path=weekly_db))
    assert [(row["department"], row["total"]) for row in rows] == [("学工办", 3), ("保卫处", 1), ("教务处", 1)]


def test_department_stats_empty() -> None:
    """空任务列表返回空统计。"""
    assert report_agent.department_stats([]) == []


def test_department_stats_skips_unassigned(weekly_db: Path) -> None:
    """未指定部门的任务不计入部门统计。"""
    rows = report_agent.department_stats(sqlite_db.list_tasks(db_path=weekly_db))
    assert all(row["department"] for row in rows)
    assert sum(row["total"] for row in rows) == 5  # 6 条任务中有 1 条无部门


def test_top_departments_helper(weekly_db: Path) -> None:
    """top_departments 是 department_stats 的投影。"""
    tasks = sqlite_db.list_tasks(db_path=weekly_db)
    assert report_agent.top_departments(tasks, top_n=2) == [
        {"department": "学工办", "count": 3},
        {"department": "保卫处", "count": 1},
    ]


# ============================================================
# 三、周报结构
# ============================================================


def test_weekly_report_title(weekly_db: Path) -> None:
    """报告以一级标题开头。"""
    assert weekly_report(weekly_db).startswith("# 本周事务闭环执行报告")


def test_weekly_report_demo_label(weekly_db: Path) -> None:
    """必须标注演示数据。"""
    assert settings.DEMO_DATA_LABEL in weekly_report(weekly_db)


def test_weekly_report_generated_at(weekly_db: Path) -> None:
    """生成时间可注入并出现在头部。"""
    assert "2026-09-23 22:30:00" in weekly_report(weekly_db)


def test_weekly_report_sections(weekly_db: Path) -> None:
    """四个章节齐全。"""
    report = weekly_report(weekly_db)
    for section in ("## 一、总体概况", "## 二、部门统计", "## 三、风险事项", "## 四、AI分析建议"):
        assert section in report


def test_weekly_report_section_count(weekly_db: Path) -> None:
    """报告中恰好有四个二级标题。"""
    report = weekly_report(weekly_db)
    assert sum(1 for line in report.splitlines() if line.startswith("## ")) == 4


def test_weekly_report_overview_metrics(weekly_db: Path) -> None:
    """总体概况包含总任务、完成率、临期、逾期。"""
    report = weekly_report(weekly_db)
    assert "| 总任务 | 6 |" in report
    assert "| 完成率 | 33.3% |" in report
    assert "| 临期 | 2 |" in report
    assert "| 逾期 | 1 |" in report
    assert "| 紧急 | 1 |" in report


def test_weekly_report_department_table(weekly_db: Path) -> None:
    """部门统计为 Markdown 表格，表头符合要求。"""
    report = weekly_report(weekly_db)
    assert "| 部门 | 总数 | 完成 | 完成率 |" in report
    assert "| 学工办 | 3 | 2 | 66.7% |" in report
    assert "| 保卫处 | 1 | 0 | 0.0% |" in report


def test_weekly_report_unassigned_note(weekly_db: Path) -> None:
    """存在未指定部门的任务时给出说明行。"""
    assert "> 另有 1 项任务未指定责任部门" in weekly_report(weekly_db)


def test_weekly_report_no_unassigned_note(healthy_db: Path) -> None:
    """没有未指定部门的任务时不出现该说明。"""
    assert "未指定责任部门" not in weekly_report(healthy_db)


def test_weekly_report_markdown_structure(weekly_db: Path) -> None:
    """报告是 Markdown：含标题、表格分隔行与列表。"""
    report = weekly_report(weekly_db)
    assert "| --- | ---: |" in report
    assert "\n- " in report
    assert "\n1. " in report


def test_weekly_report_has_no_none_literal(weekly_db: Path) -> None:
    """报告中不得出现 None（缺失信息应有占位符或提示）。"""
    assert "None" not in weekly_report(weekly_db)


def test_weekly_report_can_be_written_to_file(weekly_db: Path, tmp_path: Path) -> None:
    """报告可导出为 .md 文件且内容完整。"""
    report = weekly_report(weekly_db)
    target = tmp_path / "weekly.md"
    target.write_text(report, encoding="utf-8")

    assert target.read_text(encoding="utf-8").startswith("# 本周事务闭环执行报告")


# ============================================================
# 四、风险事项
# ============================================================


def test_risk_messages_only_urgent_and_overdue(weekly_db: Path) -> None:
    """风险事项仅包含紧急与逾期两类，不包含临期/正常任务。"""
    report = weekly_report(weekly_db)
    assert "【已逾期】宿舍消防隐患专项排查" in report
    assert "【紧急】举办机器人创新周" in report
    # 临期与已完成任务不出现在风险清单中
    assert "清理临时堆放杂物" not in report
    assert "更新宣传栏" not in report


def test_risk_messages_helper(weekly_db: Path) -> None:
    """risk_messages() 与 ReminderAgent 输出保持一致的文本。"""
    messages = reminder_agent.generate_messages(today=TODAY, db_path=weekly_db)
    risk = report_agent.risk_messages(today=TODAY, db_path=weekly_db)

    assert len(messages) == 4
    assert len(risk) == 2
    assert all(message in messages for message in risk)


def test_risk_section_empty_text(healthy_db: Path) -> None:
    """无风险时给出明确提示。"""
    report = weekly_report(healthy_db)
    assert report_tool.EMPTY_RISK_TEXT in report


def test_risk_messages_for_department(weekly_db: Path) -> None:
    """部门维度的风险清单只包含该部门事项。"""
    risk = report_agent.risk_messages(department="学工办", today=TODAY, db_path=weekly_db)
    assert len(risk) == 1
    assert "举办机器人创新周" in risk[0]


# ============================================================
# 五、AI 分析建议（规则生成）
# ============================================================


def test_suggestions_count_between_3_and_5(weekly_db: Path) -> None:
    """建议条数在 3~5 条之间。"""
    stats = report_agent.build_stats(today=TODAY, db_path=weekly_db)
    assert report_agent.MIN_SUGGESTIONS <= len(report_agent.build_suggestions(stats)) <= report_agent.MAX_SUGGESTIONS


def test_suggestion_for_overdue(weekly_db: Path) -> None:
    """逾期 > 0 → 建议优先处理逾期事项。"""
    assert report_agent.MSG_OVERDUE in weekly_report(weekly_db)


def test_no_overdue_suggestion_when_healthy(healthy_db: Path) -> None:
    """无逾期时不出现逾期建议。"""
    report = weekly_report(healthy_db)
    assert report_agent.MSG_OVERDUE not in report


def test_suggestion_for_low_completion_rate(weekly_db: Path) -> None:
    """完成率 < 70% → 建议加强督办力度。"""
    assert report_agent.MSG_SUPERVISE in weekly_report(weekly_db)


def test_no_supervise_suggestion_when_rate_ok(healthy_db: Path) -> None:
    """完成率达标时不出现督办建议，改为达标提示。"""
    report = weekly_report(healthy_db)
    assert report_agent.MSG_SUPERVISE not in report
    assert report_agent.MSG_RATE_OK.format(rate="80.0%") in report


def test_suggestion_for_full_completion(complete_db: Path) -> None:
    """完成率 100% → 输出满分提示。"""
    assert report_agent.MSG_RATE_PERFECT in weekly_report(complete_db)


def test_suggestion_for_urgent_meeting(urgent_db: Path) -> None:
    """紧急 > 3 → 建议召开专项协调会。"""
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=urgent_db)
    assert dashboard["urgent"] == 4
    assert report_agent.MSG_COORDINATION in weekly_report(urgent_db)


def test_suggestion_for_few_urgent(weekly_db: Path) -> None:
    """紧急 ≤ 3 时不建议开会，改为跟进提示。"""
    report = weekly_report(weekly_db)
    assert report_agent.MSG_COORDINATION not in report
    assert report_agent.MSG_URGENT_FOLLOWUP.format(count=1) in report


def test_suggestion_for_missing_owner(weekly_db: Path) -> None:
    """负责人缺失时给出补充建议。"""
    assert report_agent.MSG_MISSING_OWNER.format(count=2) in weekly_report(weekly_db)


def test_suggestions_capped_at_max(weekly_db: Path) -> None:
    """候选建议超过上限时按优先级截断为 5 条。"""
    report = weekly_report(weekly_db)
    suggestions = report_agent.build_suggestions(report_agent.build_stats(today=TODAY, db_path=weekly_db))

    assert len(suggestions) == report_agent.MAX_SUGGESTIONS
    assert suggestions[0] == report_agent.MSG_OVERDUE
    # 部门缺失与待确认建议被截断
    assert report_agent.MSG_MISSING_DEPARTMENT.format(count=1) not in report


def test_suggestions_padded_to_minimum(complete_db: Path) -> None:
    """规则命中不足时用通用建议补足到 3 条。"""
    suggestions = report_agent.build_suggestions(
        report_agent.build_stats(today=TODAY, db_path=complete_db)
    )
    assert len(suggestions) == report_agent.MIN_SUGGESTIONS
    assert suggestions[0] == report_agent.MSG_RATE_PERFECT
    # 其余为通用建议，属于预置文案池
    assert all(item in report_agent.FILLER_SUGGESTIONS for item in suggestions[1:])


def test_no_data_suggestion_when_empty(empty_db: Path) -> None:
    """空数据库给出数据提示，且仍有 3 条建议。"""
    report = weekly_report(empty_db)
    assert report_agent.MSG_NO_DATA in report
    suggestions = report_agent.build_suggestions(report_agent.build_stats(today=TODAY, db_path=empty_db))
    assert len(suggestions) == report_agent.MIN_SUGGESTIONS


def test_suggestions_rendered_as_ordered_list(weekly_db: Path) -> None:
    """建议渲染为有序列表。"""
    report = weekly_report(weekly_db)
    section = report.split("## 四、AI分析建议")[1]
    assert "1. " in section
    assert "2. " in section


def test_suggestions_are_deterministic(weekly_db: Path) -> None:
    """同样数据两次生成结果一致（纯规则，无随机性）。"""
    first = report_agent.build_suggestions(report_agent.build_stats(today=TODAY, db_path=weekly_db))
    second = report_agent.build_suggestions(report_agent.build_stats(today=TODAY, db_path=weekly_db))
    assert first == second


# ============================================================
# 六、部门报告
# ============================================================


def department_report(db_path: Path, department: str) -> str:
    """渲染部门报告（固定基准日期与生成时间）。"""
    return report_agent.generate_department_report(
        department, today=TODAY, db_path=db_path, generated_at=GENERATED_AT
    )


def test_department_report_title(weekly_db: Path) -> None:
    """标题包含部门名称。"""
    assert department_report(weekly_db, "学工办").startswith("# 学工办 事务闭环执行报告")


def test_department_report_only_that_department(weekly_db: Path) -> None:
    """只统计该部门任务。"""
    report = department_report(weekly_db, "学工办")
    assert "| 总任务 | 3 |" in report
    assert "| 完成率 | 66.7% |" in report
    assert "宿舍消防隐患专项排查" not in report      # 属于保卫处


def test_department_report_task_table(weekly_db: Path) -> None:
    """任务明细表包含该部门的全部任务。"""
    report = department_report(weekly_db, "学工办")
    assert "## 二、任务明细" in report
    assert "| 标题 | 状态 | 负责人 | 截止时间 | 优先级 |" in report
    for title in ("举办机器人创新周", "更新宣传栏", "整理活动总结材料"):
        assert title in report


def test_department_report_task_table_labels(weekly_db: Path) -> None:
    """状态与优先级以中文展示。"""
    report = department_report(weekly_db, "学工办")
    assert "| 已完成 |" in report
    assert "| 进行中 |" in report
    assert "| 中 |" in report


def test_department_report_risk_only_department(weekly_db: Path) -> None:
    """风险事项只列本部门的紧急 / 逾期任务。"""
    report = department_report(weekly_db, "学工办")
    assert "【紧急】举办机器人创新周" in report
    assert "宿舍消防隐患专项排查" not in report


def test_department_report_completion_rate(weekly_db: Path) -> None:
    """部门完成率与 department_stats 一致。"""
    report = department_report(weekly_db, "学工办")
    assert "| 完成率 | 66.7% |" in report


def test_department_report_unknown_department(weekly_db: Path) -> None:
    """未知部门：统计为 0，给出数据提示且不报错。"""
    report = department_report(weekly_db, "不存在的部门")
    assert "| 总任务 | 0 |" in report
    assert report_agent.MSG_NO_DATA in report
    assert report_tool.EMPTY_RISK_TEXT in report


def test_department_report_sections_and_header(weekly_db: Path) -> None:
    """部门报告同样包含四个章节与演示数据标注。"""
    report = department_report(weekly_db, "保卫处")
    for section in ("## 一、总体概况", "## 二、任务明细", "## 三、风险事项", "## 四、AI分析建议"):
        assert section in report
    assert settings.DEMO_DATA_LABEL in report
    assert "2026-09-23 22:30:00" in report


def test_department_report_suggestions_count(weekly_db: Path) -> None:
    """部门报告的建议同样为 3~5 条。"""
    stats = report_agent.build_stats(department="保卫处", today=TODAY, db_path=weekly_db)
    suggestions = report_agent.build_suggestions(stats)
    assert report_agent.MIN_SUGGESTIONS <= len(suggestions) <= report_agent.MAX_SUGGESTIONS


# ============================================================
# 七、数据来源约束（禁止重算 Reminder）
# ============================================================


def test_report_uses_reminder_agent_outputs(monkeypatch: pytest.MonkeyPatch, weekly_db: Path) -> None:
    """报告生成必须调用 count_levels() 与 generate_messages()。"""
    calls: dict[str, dict] = {}

    real_count_levels = reminder_agent.count_levels
    real_generate_messages = reminder_agent.generate_messages

    def spy_count_levels(*args: object, **kwargs: object) -> dict:
        calls["count_levels"] = dict(kwargs)
        return real_count_levels(*args, **kwargs)

    def spy_generate_messages(*args: object, **kwargs: object) -> list[str]:
        calls["generate_messages"] = dict(kwargs)
        return real_generate_messages(*args, **kwargs)

    monkeypatch.setattr(reminder_agent, "count_levels", spy_count_levels)
    monkeypatch.setattr(reminder_agent, "generate_messages", spy_generate_messages)

    report_agent.generate_weekly_report(today=TODAY, db_path=weekly_db, generated_at=GENERATED_AT)

    assert "count_levels" in calls
    assert "generate_messages" in calls
    assert calls["count_levels"]["db_path"] == weekly_db


def test_report_does_not_use_check_all(monkeypatch: pytest.MonkeyPatch, weekly_db: Path) -> None:
    """报告不调用 check_all()（允许的数据来源只有 count_levels 与 generate_messages）。"""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("ReportAgent 不得调用 check_all()")

    monkeypatch.setattr(reminder_agent, "check_all", forbidden)

    report = weekly_report(weekly_db)
    assert "| 逾期 | 1 |" in report
    assert "【已逾期】宿舍消防隐患专项排查" in report


def test_report_agent_contains_no_level_computation() -> None:
    """静态检查：报告模块的源码中不存在任何等级或天数计算逻辑。"""
    source = inspect.getsource(report_agent)
    for forbidden_name in ("compute_level", "compute_days_left", "_to_reminder"):
        assert forbidden_name not in source


def test_report_reads_tasks_from_database(monkeypatch: pytest.MonkeyPatch, weekly_db: Path) -> None:
    """任务明细必须通过 list_tasks() 获取。"""
    calls: list[dict] = []
    real_list_tasks = sqlite_db.list_tasks

    def spy_list_tasks(*args: object, **kwargs: object) -> list[dict]:
        calls.append(dict(kwargs))
        return real_list_tasks(*args, **kwargs)

    monkeypatch.setattr(sqlite_db, "list_tasks", spy_list_tasks)
    report_agent.generate_weekly_report(today=TODAY, db_path=weekly_db, generated_at=GENERATED_AT)

    assert calls
    assert calls[0]["db_path"] == weekly_db


# ============================================================
# 八、渲染工具
# ============================================================


def test_render_metrics_table() -> None:
    """指标表渲染。"""
    text = report_tool.render_metrics_table({"总任务": 6, "完成率": "33.3%"})
    assert "| 指标 | 数值 |" in text
    assert "| 总任务 | 6 |" in text


def test_render_department_table() -> None:
    """部门表渲染（含完成率百分号）。"""
    text = report_tool.render_department_table(
        [{"department": "学工办", "total": 3, "completed": 2, "completion_rate": 66.7}]
    )
    assert "| 部门 | 总数 | 完成 | 完成率 |" in text
    assert "| 学工办 | 3 | 2 | 66.7% |" in text


def test_render_task_table_placeholders() -> None:
    """缺失字段用占位符显示，不出现 None。"""
    text = report_tool.render_task_table(
        [{"title": "任务", "status": "pending", "owner": None, "deadline": None, "priority": None}]
    )
    assert "None" not in text
    assert report_tool.PLACEHOLDER in text
    assert "待办" in text


def test_render_bullet_list_empty_text() -> None:
    """空列表返回给定提示。"""
    assert report_tool.render_bullet_list([], "暂无内容。") == "暂无内容。"
    assert report_tool.render_bullet_list(["甲", "乙"]) == "- 甲\n- 乙"


def test_render_suggestions() -> None:
    """建议渲染为有序列表。"""
    assert report_tool.render_suggestions(["第一条", "第二条"]) == "1. 第一条\n2. 第二条"
    assert report_tool.render_suggestions([]) == ""


def test_format_percent_and_timestamp() -> None:
    """格式化辅助函数。"""
    assert report_tool.format_percent(33.34) == "33.3%"
    assert report_tool.format_timestamp(GENERATED_AT) == "2026-09-23 22:30:00"


def test_render_report_header() -> None:
    """报告头部包含标题、生成时间与演示数据标注。"""
    header = report_tool.render_report_header("测试报告", generated_at=GENERATED_AT)
    assert header.startswith("# 测试报告")
    assert "生成时间：2026-09-23 22:30:00" in header
    assert settings.DEMO_DATA_LABEL in header
