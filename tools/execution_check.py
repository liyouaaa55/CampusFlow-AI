"""v3.0 第二阶段：执行反馈与持续督办的统一业务层。

职责（第四页与第五页共用同一数据来源，避免两套矛盾口径）：
    1. save_feedback()      录入执行反馈：追加反馈 + 同步任务状态（闭环任务拒绝）
    2. build_task_rows()    统一的「任务 × 反馈 × 下发 × 闭环」宽表（第四/五页共用）
    3. build_overview()     统一的统计口径（见下方「统计口径」）
    4. supervision_items()  持续督办：规则层识别需要关注的任务（问题 → 依据 → 建议）

统计口径（全项目唯一，报告与页面都以此为准）
--------------------------------------------
    任务总数          tasks 表中全部任务
    已下发            存在 dispatch 链路日志的任务（与 submission_check.is_dispatched 同源）
    已完成            tasks.status == "completed"（执行状态，可被反馈同步更新）
    进行中            tasks.status == "processing"
    未反馈            已下发 且 task_feedback 中该任务没有任何记录
    逾期未完成        截止时间 < 今天 且 任务状态 != completed
    逾期后完成        截止时间 < 今天 且 任务状态 == completed
    （历史事实，二者单列，绝不合并成一个数字）
    已正式闭环        存在 task_closures 记录（与闭环保护同一判定）

三条不可让步的口径纪律：
    - **有反馈 ≠ 已完成**：反馈状态与任务状态是两个维度，任务状态只由反馈内容驱动；
    - **status == completed ≠ 正式闭环**：闭环仍以 task_closures 为准；
    - **已闭环任务不再督办**：闭环任务不出现在持续督办清单里。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping

from agent import reminder_agent
from config import settings
from database import closure_store, feedback_store, models, sqlite_db, submission_store
from tools import submission_check

# 未反馈（展示态，不入库）
FEEDBACK_NONE: str = "none"

# 反馈状态 → 任务状态 的同步规则
FEEDBACK_STATUS_TO_TASK_STATUS: dict[str, str] = {
    models.FEEDBACK_COMPLETED: "completed",
    models.FEEDBACK_IN_PROGRESS: "processing",
}

# 已闭环任务拒绝写入反馈
CLOSED_REASON: str = "该任务已正式闭环，不能再录入执行反馈（闭环保护）。"

# 持续督办类型（顺序即展示优先级）
SUPERVISION_OVERDUE: str = "overdue_unfinished"          # 已逾期且未完成
SUPERVISION_NOT_FEEDBACK: str = "not_feedback"           # 已下发但未反馈
SUPERVISION_DUE_SOON: str = "due_soon_unfinished"        # 临期且未完成
SUPERVISION_FEEDBACK_UNFINISHED: str = "feedback_unfinished"  # 已反馈但仍未完成
SUPERVISION_STALE: str = "stale_progress"                # 有督办记录却无新进展

SUPERVISION_TYPES: tuple[str, ...] = (
    SUPERVISION_OVERDUE,
    SUPERVISION_NOT_FEEDBACK,
    SUPERVISION_DUE_SOON,
    SUPERVISION_FEEDBACK_UNFINISHED,
    SUPERVISION_STALE,
)

SUPERVISION_LABELS: dict[str, str] = {
    SUPERVISION_OVERDUE: "已逾期未完成",
    SUPERVISION_NOT_FEEDBACK: "已下发未反馈",
    SUPERVISION_DUE_SOON: "临期未完成",
    SUPERVISION_FEEDBACK_UNFINISHED: "已反馈仍未完成",
    SUPERVISION_STALE: "长期无实质进展",
}

SUPERVISION_SUGGESTIONS: dict[str, str] = {
    SUPERVISION_OVERDUE: "建议立即联系负责人核实进展，并明确补救时间。",
    SUPERVISION_NOT_FEEDBACK: "建议提醒负责人按格式提交执行反馈。",
    SUPERVISION_DUE_SOON: "建议提前跟进，避免到期转为逾期。",
    SUPERVISION_FEEDBACK_UNFINISHED: "建议确认剩余工作与卡点，必要时调整计划。",
    SUPERVISION_STALE: "建议要求负责人说明具体情况，并记录下一步安排。",
}

# 已产生过处置动作的事件（用于判断「有督办记录却无新进展」）
DISPOSAL_EVENTS: frozenset[str] = frozenset(
    {
        models.ACTION_DRAFT,
        models.ACTION_REQUEST,
        models.ACTION_ESCALATE,
        models.ACTION_CONFIRM,
    }
)

# 需要关注的临期窗口（天）
DUE_SOON_DAYS: int = settings.REMINDER_DUE_SOON_DAYS


class ExecutionCheckError(RuntimeError):
    """执行检查业务异常基类。"""


# ============================================================
# 一、录入反馈（追加 + 状态同步 + 闭环保护）
# ============================================================


def save_feedback(
    task_id: int,
    *,
    status: str,
    note: str = "",
    file_path: str | Path | None = None,
    operator: str = models.DEFAULT_OPERATOR,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """保存一条执行反馈，并按统一规则同步任务状态。

    参数：
        task_id   任务主键
        status    models.FEEDBACK_COMPLETED（已完成）/ FEEDBACK_IN_PROGRESS（进行中）
        note      工作说明（选填）
        file_path 附件（选填）
        operator  录入人（比赛版由管理者代录）

    返回：{ok, reason, feedback, task, status_applied}
        - ok=False 且 reason 非空表示被拒绝（如已闭环）；
        - status_applied 为本次真正写入的任务状态（未变更时为 None）。

    规则：
        已完成    → 任务状态置为 completed（若原本就是 completed 则不重复写日志）
        进行中    → 若任务状态已是 completed，则更正回 processing（后续补交反馈以最新为准）
                   其余情况保持原状态，**不会**被置为 completed
        未提交反馈 → 不产生任何记录，任务保持「未反馈」

    异常：TaskNotFoundError 任务不存在；FeedbackError 反馈状态非法。
    """
    task = sqlite_db.get_task(int(task_id), db_path=db_path)
    if task is None:
        raise sqlite_db.TaskNotFoundError(f"任务不存在：id={task_id}")

    if closure_store.is_closed(int(task_id), db_path=db_path):
        return {
            "ok": False,
            "reason": CLOSED_REASON,
            "feedback": None,
            "task": task,
            "status_applied": None,
        }

    record = feedback_store.append_feedback(
        int(task_id),
        status,
        note=note,
        file_path=file_path,
        operator=operator,
        db_path=db_path,
    )

    current = str(task.get("status") or "")
    target = FEEDBACK_STATUS_TO_TASK_STATUS[status]
    applied: str | None = None
    try:
        if target == "completed" and current != "completed":
            sqlite_db.update_status(
                int(task_id), "completed", operator=operator,
                remark=f"执行反馈：已完成（反馈#{record['id']}）", db_path=db_path,
            )
            applied = "completed"
        elif target == "processing" and current == "completed":
            sqlite_db.update_status(
                int(task_id), "processing", operator=operator,
                remark=f"执行反馈：更正为进行中（反馈#{record['id']}）", db_path=db_path,
            )
            applied = "processing"
    except sqlite_db.DatabaseError as exc:  # 并发冲突等：反馈已留痕，如实报告状态未同步
        return {
            "ok": False,
            "reason": f"反馈已记录，但任务状态同步失败：{exc}",
            "feedback": record,
            "task": sqlite_db.get_task(int(task_id), db_path=db_path),
            "status_applied": None,
        }

    return {
        "ok": True,
        "reason": "",
        "feedback": record,
        "task": sqlite_db.get_task(int(task_id), db_path=db_path),
        "status_applied": applied,
    }


# ============================================================
# 二、统一宽表（第四页与第五页共用）
# ============================================================


def build_task_rows(
    today: date | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """构建统一的「任务 × 反馈 × 下发 × 闭环」宽表。

    每行字段（缺失一律显式 None / False，绝不猜测）：
        task_id / title / department / owner / deadline / status / priority
        dispatched / submitted / submitted_at
        feedback_status（none 表示未反馈）/ feedback_note / feedback_at
        closed / overdue / overdue_unfinished / days_left
        disposal_events（历史处置动作，供「长期无进展」判断）
    """
    base_date = today or date.today()
    tasks = sqlite_db.list_tasks(db_path=db_path)
    feedback_map = feedback_store.latest_feedback_map(db_path=db_path)
    closed_ids = {int(item["task_id"]) for item in closure_store.list_closures(db_path=db_path)}
    submissions = {
        int(row["task_id"]): row for row in submission_store.list_submissions(db_path=db_path)
    }

    rows: list[dict[str, Any]] = []
    for task in tasks:
        task_id = int(task["id"])
        deadline = task.get("deadline")
        days_left = reminder_agent.compute_days_left(deadline, base_date)
        status = str(task.get("status") or "")
        overdue = days_left is not None and days_left < 0
        feedback = feedback_map.get(task_id)
        submission = submissions.get(task_id)

        rows.append(
            {
                "task_id": task_id,
                "title": str(task.get("title") or ""),
                "department": task.get("department"),
                "owner": task.get("owner"),
                "deadline": deadline,
                "status": status,
                "priority": task.get("priority"),
                "dispatched": submission_check.is_dispatched(task_id, db_path=db_path),
                "submitted": submission is not None,
                "submitted_at": (submission or {}).get("submitted_at"),
                "feedback_status": (feedback or {}).get("status") or FEEDBACK_NONE,
                "feedback_note": (feedback or {}).get("note"),
                "feedback_at": (feedback or {}).get("created_at"),
                "feedback_file": (feedback or {}).get("file_path"),
                "closed": task_id in closed_ids,
                "overdue": overdue,
                "overdue_unfinished": bool(overdue) and status != "completed",
                "days_left": days_left,
                "disposal_events": _disposal_events(task_id, db_path),
            }
        )
    return rows


def _disposal_events(task_id: int, db_path: str | Path | None) -> list[str]:
    """该任务历史中出现过的处置事件（只读，用于「长期无进展」判断）。"""
    actions = [str(log.get("action") or "") for log in sqlite_db.get_logs(task_id, db_path=db_path)]
    return [action for action in actions if action in DISPOSAL_EVENTS]


# ============================================================
# 三、统一统计口径
# ============================================================


def build_overview(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按统一口径汇总统计（第四页 KPI 与第五页报告共用）。"""
    total = len(rows)
    dispatched = sum(1 for row in rows if row["dispatched"])
    completed = sum(1 for row in rows if row["status"] == "completed")
    processing = sum(1 for row in rows if row["status"] == "processing")
    unfeedback = sum(
        1
        for row in rows
        if row["dispatched"]
        and row["feedback_status"] == FEEDBACK_NONE
        and row["status"] != "completed"
    )
    overdue_unfinished = sum(1 for row in rows if row["overdue_unfinished"] and not row["closed"])
    overdue_completed = sum(
        1 for row in rows if row["overdue"] and row["status"] == "completed"
    )
    closed = sum(1 for row in rows if row["closed"])
    return {
        "total": total,
        "dispatched": dispatched,
        "completed": completed,
        "processing": processing,
        "unfeedback": unfeedback,
        "overdue_unfinished": overdue_unfinished,
        "overdue_completed": overdue_completed,
        "closed": closed,
        "completion_rate": round(completed * 100 / total, 1) if total else 0.0,
    }


# ============================================================
# 四、持续督办（规则层，不使用模型，不虚构历史）
# ============================================================


def supervision_items(
    today: date | None = None,
    db_path: str | Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """识别执行期间需要关注的任务。

    每项返回：task_id / kind / tag / problem / evidence / suggestion / tone

    判定只用真实字段（任务状态、截止时间、最新有效反馈、下发与处置日志），
    **已正式闭环的任务一律排除**（闭环保护）。
    """
    base_date = today or date.today()
    data = rows if rows is not None else build_task_rows(today=base_date, db_path=db_path)

    items: list[dict[str, Any]] = []
    for row in data:
        if row["closed"]:
            continue
        kind = _classify(row, base_date)
        if kind is None:
            continue
        items.append(
            {
                "task_id": row["task_id"],
                "kind": kind,
                "tag": SUPERVISION_LABELS[kind],
                "problem": SUPERVISION_LABELS[kind],
                "evidence": _evidence(row, kind),
                "suggestion": SUPERVISION_SUGGESTIONS[kind],
                "tone": "danger" if kind == SUPERVISION_OVERDUE else "warning",
                "title": row["title"],
                "department": row["department"],
                "owner": row["owner"],
            }
        )

    order = {kind: index for index, kind in enumerate(SUPERVISION_TYPES)}
    items.sort(key=lambda item: (order.get(item["kind"], 99), item["task_id"]))
    return items


def _classify(row: Mapping[str, Any], base_date: date) -> str | None:
    """按紧急程度归入**唯一**的督办类型；无需关注返回 None。

    优先级（先命中先返回，避免同一任务被重复计数）：
        1. 已逾期（未完成）—— 已逾期且已反馈过则标为「已反馈仍未完成」
        2. 临期未完成 —— 时间压力最直接
        3. 已下发未反馈 —— 执行进展不透明
        4. 长期无实质进展 —— 有督办记录却仍无反馈
    """
    status = str(row.get("status") or "")
    feedback = str(row.get("feedback_status") or FEEDBACK_NONE)
    days_left = row.get("days_left")

    if row.get("overdue_unfinished"):
        if feedback == models.FEEDBACK_IN_PROGRESS:
            return SUPERVISION_FEEDBACK_UNFINISHED
        return SUPERVISION_OVERDUE

    if isinstance(days_left, int) and 0 <= days_left <= DUE_SOON_DAYS and status != "completed":
        return SUPERVISION_DUE_SOON

    if row.get("dispatched") and feedback == FEEDBACK_NONE and status != "completed":
        return SUPERVISION_NOT_FEEDBACK

    if row.get("disposal_events") and feedback == FEEDBACK_NONE and status != "completed":
        return SUPERVISION_STALE

    return None


def _evidence(row: Mapping[str, Any], kind: str) -> str:
    """给出可核验的判断依据（全部来自真实字段）。"""
    days_left = row.get("days_left")
    pieces: list[str] = []
    if kind in (SUPERVISION_OVERDUE, SUPERVISION_FEEDBACK_UNFINISHED):
        pieces.append(f"已逾期 {abs(int(days_left or 0))} 天")
    elif kind == SUPERVISION_DUE_SOON:
        pieces.append(f"剩余 {int(days_left or 0)} 天到期")
    pieces.append("任务状态：" + settings.STATUS_LABELS.get(str(row.get("status")), "—"))
    pieces.append(
        "最新反馈："
        + (
            "未反馈"
            if row.get("feedback_status") == FEEDBACK_NONE
            else settings.STATUS_LABELS.get("completed", "已完成")
            if row.get("feedback_status") == models.FEEDBACK_COMPLETED
            else "进行中/暂未完成"
        )
    )
    if kind == SUPERVISION_STALE:
        pieces.append("已有督办记录：" + "、".join(row.get("disposal_events") or []))
    if not row.get("owner"):
        pieces.append("负责人未指定")
    return "；".join(pieces)
