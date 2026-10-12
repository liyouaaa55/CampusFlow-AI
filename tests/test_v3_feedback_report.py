"""v3.0 第二阶段：执行反馈、持续督办与工作检查报告。

覆盖本次改造的核心业务约束：
    1. 反馈追加留痕，且「最新有效反馈」明确可查；
    2. 反馈状态同步任务状态：已完成 → completed；进行中 → 绝不完成；
       后续补交反馈以最新为准（completed → 进行中 会被更正）；
    3. 「有反馈」不等于「已完成」；「status == completed」不等于「已正式闭环」；
    4. 已正式闭环任务拒绝写入反馈（闭环保护）；
    5. 附件与反馈一起绑定到具体任务，不串号；
    6. 持续督办只用真实字段，且排除已闭环任务；
    7. 工作检查报告正文含正式文字总结与完整任务明细表，Word/Markdown 均可导出，
       且重新生成会反映最新反馈。

全部使用 tmp_path 隔离数据库；**不调用任何大模型**。
"""

from __future__ import annotations

import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from database import (
    closure_store,
    feedback_store,
    models,
    sqlite_db,
    task_metadata_store,
)
from tools import closure_service, execution_check, task_dispatch, verification_service
from tools import work_check_report
from ui.pages import inspection_page

TODAY = date(2026, 10, 10)

# 日期基准（相对 TODAY）
DEADLINE_OVERDUE = "2026-10-03"    # 已逾期
DEADLINE_DUE_SOON = "2026-10-12"   # 剩余 2 天（临期）
DEADLINE_NORMAL = "2026-12-31"     # 远期

CONTENT = "已提交整改台账"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "v3_feedback.db"
    sqlite_db.init_db(path)
    return path


def _seed(db: Path, **overrides: Any) -> int:
    """建一条已确认任务并下发（执行期任务）。"""
    payload: dict[str, Any] = {
        "title": "实验室安全隐患整改",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": DEADLINE_NORMAL,
        "priority": "high",
        "status": "processing",
        "risk_level": "green",
        "need_confirm": False,
        "description": "整改隐患并提交台账",
    }
    payload.update(overrides)
    task_id = int(sqlite_db.create_task(payload, db_path=db)["id"])
    if payload.get("dispatch", True):
        task_dispatch.dispatch_task(task_id, db_path=db)
    return task_id


def _close(db: Path, task_id: int) -> None:
    """把任务推进到正式闭环。"""
    task_metadata_store.set_closure_criteria(
        task_id, ["提交整改台账"], closure_source="manual",
        closure_confirmed=True, db_path=db,
    )
    task_dispatch.submit_work(task_id, CONTENT, db_path=db)
    verification_service.verify_submission(task_id, decision="approved", db_path=db)
    outcome = closure_service.close_task(task_id, closed_by="督办员", db_path=db)
    assert outcome["ok"] is True, outcome


# ============================================================
# 一、反馈留痕与状态同步
# ============================================================


def test_feedback_completed_syncs_task_status(db: Path) -> None:
    task_id = _seed(db)

    outcome = inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_COMPLETED, note="已完成整改并归档",
        operator="李主任", db_path=db,
    )

    assert outcome["ok"] is True
    assert outcome["status_applied"] == "completed"
    assert sqlite_db.get_task(task_id, db_path=db)["status"] == "completed"
    assert feedback_store.count_feedback(task_id, db_path=db) == 1
    latest = feedback_store.latest_feedback(task_id, db_path=db)
    assert latest is not None and latest["status"] == models.FEEDBACK_COMPLETED


def test_in_progress_never_marks_completed(db: Path) -> None:
    task_id = _seed(db, status="pending")

    outcome = inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_IN_PROGRESS, note="仍在整改中", db_path=db
    )

    assert outcome["ok"] is True
    assert sqlite_db.get_task(task_id, db_path=db)["status"] == "pending"
    assert outcome["status_applied"] is None


def test_latest_feedback_prevails(db: Path) -> None:
    """先标记完成、后更正为进行中：以最新有效反馈为准。"""
    task_id = _seed(db)
    inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_COMPLETED, db_path=db
    )
    assert sqlite_db.get_task(task_id, db_path=db)["status"] == "completed"

    outcome = inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_IN_PROGRESS, note="发现遗漏，仍需返工", db_path=db
    )

    assert outcome["status_applied"] == "processing"
    assert sqlite_db.get_task(task_id, db_path=db)["status"] == "processing"
    # 轨迹保留（追加式，不覆盖）
    assert feedback_store.count_feedback(task_id, db_path=db) == 2
    assert feedback_store.latest_feedback(task_id, db_path=db)["status"] == models.FEEDBACK_IN_PROGRESS


def test_no_feedback_means_unfeedback(db: Path) -> None:
    """未提交反馈时保留「未反馈」事实，且任务状态不变。"""
    task_id = _seed(db, status="processing")
    rows = execution_check.build_task_rows(today=TODAY, db_path=db)
    overview = execution_check.build_overview(rows)

    assert feedback_store.latest_feedback(task_id, db_path=db) is None
    assert rows[0]["feedback_status"] == execution_check.FEEDBACK_NONE
    assert overview["unfeedback"] == 1
    assert overview["completed"] == 0          # 有下发 ≠ 已完成
    assert sqlite_db.get_task(task_id, db_path=db)["status"] == "processing"


def test_attachment_binds_to_its_own_task(db: Path) -> None:
    """附件与反馈绑定到具体任务，不会串到其他任务。"""
    first = _seed(db, title="任务A")
    second = _seed(db, title="任务B")

    inspection_page.run_save_feedback(
        first, status=models.FEEDBACK_IN_PROGRESS, note="A 的进展",
        file_path="uploads/20261010_a.pdf", db_path=db,
    )
    inspection_page.run_save_feedback(
        second, status=models.FEEDBACK_IN_PROGRESS, note="B 的进展", db_path=db
    )

    a = feedback_store.latest_feedback(first, db_path=db)
    b = feedback_store.latest_feedback(second, db_path=db)
    assert a["file_path"] == "20261010_a.pdf"       # 只保留文件名
    assert a["note"] == "A 的进展"
    assert b["file_path"] is None
    assert b["note"] == "B 的进展"


def test_closed_task_rejects_feedback(db: Path) -> None:
    """闭环保护：已正式闭环任务不接受新的执行反馈。"""
    task_id = _seed(db)
    _close(db, task_id)
    before = feedback_store.count_feedback(task_id, db_path=db)

    outcome = inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_COMPLETED, db_path=db
    )

    assert outcome["ok"] is False
    assert "闭环" in outcome["message"]
    assert feedback_store.count_feedback(task_id, db_path=db) == before
    assert closure_store.is_closed(task_id, db_path=db) is True


# ============================================================
# 二、统一统计口径
# ============================================================


def test_overview_separates_overdue_and_closed(db: Path) -> None:
    """逾期未完成 / 逾期后完成 / 已正式闭环 各自独立计数。"""
    overdue_open = _seed(db, title="逾期未完成", deadline=DEADLINE_OVERDUE)
    overdue_done = _seed(db, title="逾期后完成", deadline=DEADLINE_OVERDUE)
    inspection_page.run_save_feedback(
        overdue_done, status=models.FEEDBACK_COMPLETED, db_path=db
    )
    closed = _seed(db, title="已闭环任务", deadline=DEADLINE_NORMAL)
    _close(db, closed)
    normal = _seed(db, title="正常推进", deadline=DEADLINE_NORMAL)

    rows = execution_check.build_task_rows(today=TODAY, db_path=db)
    overview = execution_check.build_overview(rows)

    assert overview["total"] == 4
    assert overview["completed"] == 1                 # 仅「逾期后完成」那条
    assert overview["overdue_unfinished"] == 1        # 逾期未完成（已闭环的那条不算）
    assert overview["overdue_completed"] == 1
    assert overview["closed"] == 1                     # 以 task_closures 为准
    # 已闭环任务不在「逾期未完成」里重复计数
    closed_row = next(r for r in rows if r["task_id"] == closed)
    assert closed_row["closed"] is True
    assert normal is not None and overdue_open is not None


def test_completion_requires_explicit_completion(db: Path) -> None:
    """有反馈但反馈为「进行中」时，不得计入已完成。"""
    task_id = _seed(db)
    inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_IN_PROGRESS, db_path=db
    )
    rows = execution_check.build_task_rows(today=TODAY, db_path=db)
    overview = execution_check.build_overview(rows)

    assert rows[0]["feedback_status"] == models.FEEDBACK_IN_PROGRESS
    assert overview["completed"] == 0
    assert overview["processing"] == 1


# ============================================================
# 三、持续督办
# ============================================================


def test_supervision_covers_expected_kinds(db: Path) -> None:
    overdue = _seed(db, title="逾期未反馈", deadline=DEADLINE_OVERDUE)
    due_soon = _seed(db, title="临期未完成", deadline=DEADLINE_DUE_SOON)
    _seed(db, title="正常推进", deadline=DEADLINE_NORMAL)

    items = execution_check.supervision_items(today=TODAY, db_path=db)
    kinds = {item["task_id"]: item["kind"] for item in items}

    assert kinds[overdue] == execution_check.SUPERVISION_OVERDUE
    assert kinds[due_soon] == execution_check.SUPERVISION_DUE_SOON
    # 每条都必须给出可核验的依据，不得虚构
    for item in items:
        assert item["evidence"]
        assert item["suggestion"]


def test_supervision_excludes_closed_tasks(db: Path) -> None:
    """已正式闭环任务不参与持续督办（闭环保护）。"""
    task_id = _seed(db, deadline=DEADLINE_OVERDUE)
    _close(db, task_id)

    items = execution_check.supervision_items(today=TODAY, db_path=db)

    assert all(item["task_id"] != task_id for item in items)


def test_feedback_unfinished_is_distinguished(db: Path) -> None:
    """已反馈但仍未完成，与「未反馈」区分开。"""
    task_id = _seed(db, deadline=DEADLINE_OVERDUE)
    inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_IN_PROGRESS, note="仍在推进", db_path=db
    )

    items = execution_check.supervision_items(today=TODAY, db_path=db)

    assert items[0]["kind"] == execution_check.SUPERVISION_FEEDBACK_UNFINISHED
    assert "进行中" in items[0]["evidence"]


# ============================================================
# 四、工作检查报告
# ============================================================


def test_markdown_contains_summary_and_full_detail_table(db: Path) -> None:
    first = _seed(db, title="宿舍消防隐患整改", deadline=DEADLINE_OVERDUE)
    # 缺负责人的任务无法下发（OwnerRequiredError），因此用 dispatch=False 建库
    second = _seed(db, title="教材征订统计", deadline=DEADLINE_NORMAL, owner=None, dispatch=False)
    inspection_page.run_save_feedback(
        first, status=models.FEEDBACK_COMPLETED, note="已整改完成", db_path=db
    )

    report = work_check_report.build_report(today=TODAY, db_path=db)
    markdown = report["markdown"]

    # 正式文字总结
    assert "## 一、总体情况" in markdown
    assert "## 二、主要问题" in markdown
    assert "## 三、重点关注任务" in markdown
    assert "## 四、后续督办建议" in markdown
    assert f"共纳入 {report['overview']['total']} 项任务" in markdown
    # 完整任务明细表：两项任务都必须在表里
    assert "## 五、任务明细表" in markdown
    assert "宿舍消防隐患整改" in markdown
    assert "教材征订统计" in markdown
    # 缺失信息显式标注，不虚构
    assert "未指定负责人" in markdown
    assert second is not None


def test_report_detail_rows_have_required_columns(db: Path) -> None:
    _seed(db, title="任务X", deadline=DEADLINE_OVERDUE)
    report = work_check_report.build_report(today=TODAY, db_path=db)
    row = report["detail"][0]
    for column in work_check_report.DETAIL_COLUMNS:
        assert column in row
        assert str(row[column]).strip() != ""


def test_report_reflects_latest_feedback(db: Path) -> None:
    """第四页保存反馈后，重新生成的报告必须反映最新数据。"""
    task_id = _seed(db, deadline=DEADLINE_NORMAL)

    before = work_check_report.build_report(today=TODAY, db_path=db)
    assert before["overview"]["unfeedback"] == 1
    assert before["overview"]["completed"] == 0

    inspection_page.run_save_feedback(
        task_id, status=models.FEEDBACK_COMPLETED, note="本期已完成", db_path=db
    )

    after = work_check_report.build_report(today=TODAY, db_path=db)
    assert after["overview"]["completed"] == 1
    assert after["overview"]["unfeedback"] == 0
    assert "已完成" in after["detail"][0]["最新反馈"]
    assert "本期已完成" in after["detail"][0]["反馈说明"]


def test_docx_export_is_real_file(db: Path) -> None:
    """Word 导出必须是真实生成的 docx（可被 zip 打开），不得只返回空内容。"""
    _seed(db, title="导出验证任务", deadline=DEADLINE_NORMAL)
    report = work_check_report.build_report(today=TODAY, db_path=db)

    payload = report["docx"]
    assert isinstance(payload, (bytes, bytearray))
    assert len(payload) > 0
    assert payload[:2] == b"PK"                      # docx 本质是 zip 容器
    with zipfile.ZipFile(BytesIO(bytes(payload))) as archive:
        names = archive.namelist()
    assert "word/document.xml" in names
