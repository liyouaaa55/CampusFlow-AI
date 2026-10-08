"""v2.1 执行检查：回执状态判定与汇总统计（**纯确定性，不调用大模型**）。

为什么单独一个模块：
    「已提交 / 未提交 / 已逾期未提交」是**事实判定**，不是语言理解。
    判定依据只有两条：数据库里有没有回执记录、当前时间是否越过 deadline。
    任何需要大模型参与的地方都是设计错误——模型可能把「没提交」说成「已提交」，
    而执行检查报告是要给评委看的，数字必须可复算、可追溯。

三条判定规则（缺一不可）：
    1. 有回执记录                        → 已提交
    2. 无回执记录且未越过截止时间        → 待提交
    3. 无回执记录且已越过截止时间        → 已逾期未提交
    没有 deadline 时不判定逾期，一律按「待提交」处理（不许臆造截止日期）。

「已下发」同样不存库，而是从 task_logs 中 action=dispatch 的存在性推导，
因此重复下发不会产生第二份状态，也永远不会出现「下发记录与实际不符」。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from database import models, sqlite_db

# 回执展示态（这三个值只用于展示与统计，数据库里不存）
STATE_PENDING: str = "pending_submit"          # 待提交
STATE_SUBMITTED: str = "submitted"             # 已提交
STATE_OVERDUE: str = "overdue_unsubmitted"     # 已逾期未提交

SUBMISSION_STATES: tuple[str, ...] = (STATE_PENDING, STATE_SUBMITTED, STATE_OVERDUE)


def _parse_date(value: Any) -> date | None:
    """解析 YYYY-MM-DD；无法解析返回 None（绝不猜日期）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def is_dispatched(task_id: int, db_path: str | str | None = None) -> bool:
    """判断任务是否已下发（依据 task_logs 中是否存在 dispatch 记录）。

    从日志推导而非另存一个状态字段：这样重复点击「系统内下发」不会
    产生任何第二份状态，天然满足幂等。
    """
    for log in sqlite_db.get_logs(int(task_id), db_path=db_path):
        if log["action"] == models.ACTION_DISPATCH:
            return True
    return False


def submission_state(
    task: dict[str, Any],
    submission: dict[str, Any] | None,
    today: str | date | None = None,
) -> str:
    """判定某任务的回执状态，返回 STATE_* 之一。

    参数：
        task        任务记录（只需要 deadline）
        submission  该任务的回执记录；None 表示尚未提交
        today       基准日期；默认取系统当天（测试可注入固定值）
    """
    if submission:
        return STATE_SUBMITTED

    deadline = _parse_date(task.get("deadline"))
    if deadline is None:
        # 没有截止时间就不判定逾期——绝不臆造一个期限
        return STATE_PENDING

    if today is None:
        current = date.today()
    elif isinstance(today, date):
        current = today
    else:
        parsed = _parse_date(today)
        current = parsed if parsed else date.today()

    return STATE_OVERDUE if current > deadline else STATE_PENDING


def build_row(
    task: dict[str, Any],
    submission: dict[str, Any] | None,
    dispatched: bool,
    today: str | date | None = None,
) -> dict[str, Any]:
    """构造一条执行检查明细（纯数据，不含展示文案）。

    明细里的 owner / department / deadline 一律**原样取自任务表**，
    本模块既不补全也不推断，绝不虚构负责人或截止时间。
    """
    state = submission_state(task, submission, today=today)
    return {
        "task_id": int(task.get("id") or 0),
        "title": str(task.get("title") or ""),
        "department": str(task.get("department") or ""),
        "owner": str(task.get("owner") or ""),
        "deadline": str(task.get("deadline") or ""),
        "dispatched": bool(dispatched),
        "state": state,
        "submitted_at": str((submission or {}).get("submitted_at") or ""),
        "content": str((submission or {}).get("content") or ""),
        "file_path": str((submission or {}).get("file_path") or ""),
    }


def summarize(
    tasks: Iterable[dict[str, Any]],
    submissions: dict[int, dict[str, Any]] | None = None,
    dispatched_ids: Iterable[int] | None = None,
    today: str | date | None = None,
) -> dict[str, Any]:
    """汇总执行检查统计。

    参数：
        tasks          任务列表
        submissions    task_id -> 回执记录的映射
        dispatched_ids 已下发的 task_id 集合
        today          基准日期

    返回：含 counts / rate / rows 的字典。
    提交率 = 已提交 / 任务总数（任务总数为 0 时提交率为 0.0，不做除零）。
    """
    subs = submissions or {}
    dispatched = set(dispatched_ids or ())

    rows: list[dict[str, Any]] = []
    submitted = pending = overdue = dispatched_count = 0

    for task in tasks:
        task_id = int(task.get("id") or 0)
        row = build_row(task, subs.get(task_id), task_id in dispatched, today=today)
        rows.append(row)

        if row["dispatched"]:
            dispatched_count += 1
        if row["state"] == STATE_SUBMITTED:
            submitted += 1
        elif row["state"] == STATE_OVERDUE:
            overdue += 1
        else:
            pending += 1

    total = len(rows)
    rate = round(submitted / total * 100, 1) if total else 0.0

    return {
        "total": total,
        "dispatched": dispatched_count,
        "submitted": submitted,
        "pending": pending,
        "overdue": overdue,
        "rate": rate,
        "rows": rows,
    }
