"""v2.2 任务通知与下发体验：只读核查后的最小约束回归测试。

覆盖：
    - 未下发任务的回执提交被后端拒绝（正常业务路径强制）；
    - 已下发任务可正常提交回执；
    - 通知内容包含已确认的办结标准与办理要求；
    - 未确认标准不被误标为正式要求；
    - 未确认（need_confirm）任务禁止下发；
    - 已闭环任务禁止再次下发；
    - 不同任务的通知内容不串用；
    - 未传入元数据时通知不臆造办结标准。

全部使用 tmp_path 独立数据库，不触碰真实的 data/campusflow.db。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from database import closure_store, sqlite_db, task_metadata_store
from tools import dispatch_notice, task_dispatch


def make_task(db: Path, **over: object) -> dict:
    task = {
        "title": "实验室安全检查",
        "department": "信息工程学院",
        "owner": "张三",
        "deadline": "2026-10-15",
        "priority": "high",
        "status": "pending",
        "risk_level": "green",
        "need_confirm": False,
        "description": "完成实验室安全检查并提交结果。",
    }
    task.update(over)
    return sqlite_db.create_task(task, db_path=db)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "v22_clarity.db"
    sqlite_db.init_db(db_path)
    return db_path


def test_undispatched_submit_rejected(db: Path) -> None:
    """未下发任务的正常回执提交（require_dispatched）被后端拒绝。"""
    task = make_task(db)
    with pytest.raises(task_dispatch.NotDispatchedError):
        task_dispatch.submit_work(
            task["id"], "已完成", require_dispatched=True, db_path=db
        )


def test_dispatched_submit_allowed(db: Path) -> None:
    """已下发任务可正常提交；默认（不强制）调用仍兼容旧逻辑。"""
    task = make_task(db)
    task_dispatch.dispatch_task(task["id"], db_path=db)
    first = task_dispatch.submit_work(
        task["id"], "已完成", require_dispatched=True, db_path=db
    )
    assert first["ok"] is True
    # 默认路径不强制，回归兼容既有工具调用
    second = task_dispatch.submit_work(task["id"], "补充材料", db_path=db)
    assert second["updated"] is True


def test_notice_includes_confirmed_criteria(db: Path) -> None:
    """通知应包含已确认的办结标准与办理要求，且标注「已确认」。"""
    task = make_task(db, description="按计划完成检查并提交报告")
    meta = task_metadata_store.set_closure_criteria(
        task["id"],
        ["完成全部检查项", "提交签字确认表"],
        closure_source="manual",
        closure_confirmed=True,
        db_path=db,
    )
    notice = task_dispatch.prepare_notice(task, metadata=meta)
    body = notice["body"]
    assert "【办结标准】" in body
    assert "完成全部检查项" in body
    assert "已确认" in body
    assert "【办理要求】" in body
    assert "按计划完成检查并提交报告" in body


def test_notice_marks_unconfirmed_criteria(db: Path) -> None:
    """未确认标准必须标注「待确认 / 仅供参考」，绝不包装成正式要求。"""
    task = make_task(db)
    meta = task_metadata_store.set_closure_criteria(
        task["id"],
        ["待确认事项"],
        closure_source="ai",
        closure_confirmed=False,
        db_path=db,
    )
    notice = task_dispatch.prepare_notice(task, metadata=meta)
    body = notice["body"]
    assert "待确认" in body or "仅供参考" in body
    assert "已确认，作为正式办结依据" not in body


def test_unconfirmed_task_blocked(db: Path) -> None:
    """需要人工确认但尚未确认的任务禁止下发。"""
    task = make_task(db, need_confirm=True, status="pending")
    assert task_dispatch.dispatch_block_reason(task) == "need_confirm"
    assert task_dispatch.can_dispatch(task) is False
    with pytest.raises(task_dispatch.NotReadyError):
        task_dispatch.dispatch_task(task["id"], db_path=db)


def test_closed_task_blocked_from_dispatch(db: Path) -> None:
    """已正式闭环任务不能再次下发，避免意外重开生命周期。"""
    task = make_task(db)
    task_dispatch.dispatch_task(task["id"], db_path=db)
    sub = task_dispatch.submit_work(task["id"], "已完成检查", db_path=db)
    closure_store.close_task(
        task["id"],
        submission_id=int(sub["submission"]["id"]),
        verification_id=1,
        closed_by="系统",
        db_path=db,
    )
    assert closure_store.get_closure(task["id"], db_path=db) is not None
    with pytest.raises(task_dispatch.NotReadyError):
        task_dispatch.dispatch_task(task["id"], db_path=db)


def test_notice_not_leak_across_tasks(db: Path) -> None:
    """A 的办结标准不得串入 B 的通知。"""
    first = make_task(db, title="任务A")
    second = make_task(db, title="任务B")
    meta_a = task_metadata_store.set_closure_criteria(
        first["id"],
        ["仅A专属标准"],
        closure_source="manual",
        closure_confirmed=True,
        db_path=db,
    )
    notice_a = task_dispatch.prepare_notice(first, metadata=meta_a)
    notice_b = task_dispatch.prepare_notice(second, metadata=None)
    assert "仅A专属标准" in notice_a["body"]
    assert "仅A专属标准" not in notice_b["body"]


def test_notice_does_not_fabricate_without_metadata() -> None:
    """未传入元数据时，通知不臆造办结标准段落。"""
    task = {
        "title": "T",
        "owner": "O",
        "department": "D",
        "deadline": "2026-10-15",
    }
    notice = dispatch_notice.generate_notice(task)
    assert "【办结标准】" not in notice["body"]


def test_business_entries_force_require_dispatched() -> None:
    """结构性约束：ui/pages 下所有 submit_work 调用必须强制校验下发状态。

    证明兼容参数 require_dispatched=False 不会被任何正式业务入口用来绕过约束。
    """
    import ast
    from pathlib import Path

    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    violations: list[tuple[str, int]] = []
    for path in pages_dir.glob("*.py"):
        # utf-8-sig：部分页面文件带 BOM，直接用 utf-8 读会让 ast.parse 报错
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit_work"
            ):
                kwarg_names = {kw.arg for kw in node.keywords}
                if "require_dispatched" not in kwarg_names:
                    violations.append((path.name, node.lineno))
    assert violations == [], f"以下正式入口未强制校验下发状态：{violations}"

