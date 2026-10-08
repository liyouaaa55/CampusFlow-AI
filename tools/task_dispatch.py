"""v2.1 执行闭环编排：系统内任务下发 + 负责人工作回执。

本模块是页面与数据层之间的唯一入口，负责三件事：
    1. 下发前置校验（负责人是硬门槛）；
    2. 系统内下发（幂等，重复点击不产生第二条记录）；
    3. 提交工作回执（upsert，绝不产生第二条 Submission）。

三条贯穿全模块的纪律：
    1. **只在本系统内流转**。这里的「下发」只是 CampusFlow 内部的状态变化，
       不调用微信 / 企业微信 / OA / 邮件等任何外部通道，也绝不伪造外部发送结果。
       返回文案统一是「已在系统内下发」，不会出现任何外部平台字样。
    2. **一切以传入的 task_id 为准**，绝不依赖 session_state 里的旧任务。
       每个入口都重新按 task_id 读库，任务不存在直接报错，从根上杜绝串任务。
    3. **不修改 tasks 表**。下发只写 task_logs，回执只写 task_submissions，
       任务本身的字段一个字都不动。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from database import models, sqlite_db, submission_store
from database.sqlite_db import TaskNotFoundError, append_chain_log
from tools import dispatch_notice, submission_check

LlmFn = Callable[[str, str], Any]

# 下发操作人（系统内动作，不冒用具体人名）
DISPATCH_OPERATOR = "系统"


class DispatchError(RuntimeError):
    """下发 / 回执流程的业务异常基类。"""


class OwnerRequiredError(DispatchError):
    """任务未指定负责人，禁止下发。"""


class ContentRequiredError(DispatchError):
    """回执正文为空。"""


def can_dispatch(task: dict[str, Any]) -> bool:
    """是否允许下发（负责人非空）。"""
    return dispatch_notice.dispatch_ready(task)


def prepare_notice(
    task: dict[str, Any],
    llm_fn: LlmFn | None = None,
) -> dict[str, Any]:
    """生成下发信息（供页面预览，不落库、不改变任务）。

    即使 LLM 不可用也一定能返回一份可展示的通知（见 dispatch_notice）。
    """
    if not can_dispatch(task):
        raise OwnerRequiredError(dispatch_notice.missing_owner_message())
    return dispatch_notice.generate_notice(task, llm_fn=llm_fn)


# ============================================================
# 一、系统内下发
# ============================================================


def dispatch_task(
    task_id: int,
    operator: str = DISPATCH_OPERATOR,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """系统内下发任务。

    幂等性：重复调用**不会**产生第二条日志，而是直接返回 already=True，
    由页面提示「该任务已下发」。

    边界：
    - 任务不存在 → TaskNotFoundError；
    - owner 为空 → OwnerRequiredError（绝不猜测负责人）；
    - 只写 task_logs，不修改 tasks 表任何字段。
    """
    task = _load_task(task_id, db_path)

    if not can_dispatch(task):
        raise OwnerRequiredError(dispatch_notice.missing_owner_message())

    if submission_check.is_dispatched(int(task_id), db_path=db_path):
        return {
            "ok": True,
            "already": True,
            "task_id": int(task_id),
            "message": "该任务已下发，无需重复操作。",
        }

    owner = str(task.get("owner") or "").strip()
    append_chain_log(
        task_id=int(task_id),
        action=models.ACTION_DISPATCH,
        operator=operator or DISPATCH_OPERATOR,
        remark=f"已在系统内下发给 {owner}",
        db_path=db_path,
    )

    return {
        "ok": True,
        "already": False,
        "task_id": int(task_id),
        "message": f"已在系统内下发给 {owner}。",
    }


# ============================================================
# 二、工作回执
# ============================================================


def submit_work(
    task_id: int,
    content: str,
    file_path: str | Path | None = None,
    submitted_at: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """提交 / 更新一条工作回执。

    唯一性：task_submissions.task_id 上有 UNIQUE 约束，这里再叠加 upsert
    语义，因此**重复提交不会产生第二条记录**，而是更新原记录并刷新提交时间。

    边界：
    - 任务不存在 → TaskNotFoundError（不依赖任何页面缓存的旧任务）；
    - 正文为空 → ContentRequiredError；
    - 不修改 tasks 表，不触碰 task.status。
    """
    _load_task(task_id, db_path)

    text = str(content or "").strip()
    if not text:
        raise ContentRequiredError("完成情况不能为空，请填写后提交。")

    existing = submission_store.get_submission(int(task_id), db_path=db_path)
    record = submission_store.save_submission(
        task_id=int(task_id),
        content=text,
        file_path=file_path,
        submitted_at=submitted_at,
        db_path=db_path,
    )

    # 只有「首次提交」才写 submit 日志；补填内容不重复刷事件链
    if existing is None:
        append_chain_log(
            task_id=int(task_id),
            action=models.ACTION_SUBMIT,
            operator=models.DEFAULT_OPERATOR,
            remark="负责人已提交工作回执",
            db_path=db_path,
        )

    return {
        "ok": True,
        "updated": existing is not None,
        "task_id": int(task_id),
        "submission": record,
        "message": "回执已更新。" if existing is not None else "工作回执已提交。",
    }


# ============================================================
# 三、查询视图
# ============================================================


def task_check_view(
    task_id: int,
    today: str | date | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """工作检查窗口的数据视图。

    **每次都按 task_id 重新读库**，绝不返回上一次任务的残留数据——
    这是「防串任务」的最后一道保障，页面每次进入都必须调用本函数。
    """
    task = _load_task(task_id, db_path)
    submission = submission_store.get_submission(int(task_id), db_path=db_path)
    dispatched = submission_check.is_dispatched(int(task_id), db_path=db_path)
    state = submission_check.submission_state(task, submission, today=today)

    return {
        "task": task,
        "submission": submission,
        "dispatched": dispatched,
        "state": state,
    }


def summary(
    status: str | None = None,
    department: str | None = None,
    today: str | date | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """执行检查汇总（确定性统计，不调用大模型）。"""
    tasks = sqlite_db.list_tasks(status=status, department=department, db_path=db_path)
    submissions = {
        int(row["task_id"]): row
        for row in submission_store.list_submissions(db_path=db_path)
    }
    dispatched_ids = [
        int(task["id"])
        for task in tasks
        if submission_check.is_dispatched(int(task["id"]), db_path=db_path)
    ]
    return submission_check.summarize(
        tasks, submissions, dispatched_ids=dispatched_ids, today=today
    )


# ============================================================
# 四、内部
# ============================================================


def _load_task(task_id: int, db_path: str | Path | None) -> dict[str, Any]:
    """按 task_id 读取任务；不存在直接抛错，绝不回退到任何缓存数据。"""
    task = sqlite_db.get_task(int(task_id), db_path=db_path)
    if task is None:
        raise TaskNotFoundError(f"任务不存在：id={task_id}")
    return task
