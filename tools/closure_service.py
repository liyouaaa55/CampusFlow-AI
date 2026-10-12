"""v2.2 正式闭环服务：前置校验 + 闭环 + 幂等 + 审计日志。

与既有模块的边界（互不替代）：
    submission_check    「交没交？」      纯事实投影
    verification_service「算不算办结？」   业务核验，产出 verifications
    本模块              「正式办结没有？」  督办人员显式闭环，产出 task_closures

**核验通过 ≠ 已闭环。** 本模块是唯一有权限写 task_closures 的地方，
且必须由督办人员显式触发（authorized_by）。

四道防线（任一不满足即拒绝，不写任何数据）：
    1. 版本绑定：approved 核验必须属于**当前提交内容**（指纹一致），
       旧版本的通过结论不能用于新版本；
    2. 标准合规：办结标准非空、已确认、且来源不是 AI；
    3. 无未决结论：当前版本不得存在 need_supplement / rejected / manual_review；
    4. 状态允许：任务不得处于已取消 / 延期等例外态。
高风险（risk_level=red）额外要求督办人员显式授权理由。

不改 tasks.status，不改 task_submissions.status，不重开（不提供 reopen）。
"""

from __future__ import annotations

from typing import Any

from config import settings
from database import closure_store, models, sqlite_db
from database import submission_store, task_metadata_store, verification_store

# 会阻止闭环的核验结论（针对当前提交版本）
BLOCKING_STATUSES: tuple[str, ...] = (
    models.VERIFICATION_NEED_SUPPLEMENT,
    models.VERIFICATION_REJECTED,
    models.VERIFICATION_MANUAL_REVIEW,
)

# 任务处于这些状态时不得闭环（例外事项不得伪装成正常办结）
BLOCKED_TASK_STATUSES: tuple[str, ...] = ("overdue",)

# 合规的办结标准来源（AI 来源不得作为强制闭环依据）
ALLOWED_CLOSURE_SOURCES: tuple[str, ...] = (
    models.CLOSURE_SOURCE_MATERIAL,
    models.CLOSURE_SOURCE_RULE,
    models.CLOSURE_SOURCE_MANUAL,
)

HIGH_RISK: str = "red"

def _reject(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "closed": False, "reason": reason, "closure": None, **extra}


def close_task(
    task_id: int,
    *,
    closed_by: str,
    closure_note: str = "",
    high_risk_ack: str = "",
    db_path: Any = None,
) -> dict[str, Any]:
    """由督办人员显式执行正式闭环。

    参数：
        task_id         任务主键
        closed_by       闭环操作人（必填，不允许空）
        closure_note    闭环说明
        high_risk_ack   高风险任务的显式授权理由；为空则高风险任务拒绝闭环
        db_path         数据库路径

    返回：{ok, closed, reason, closure, ...}
        ok=False 时 reason 给出**具体**未通过的前置条件，closure 为 None。
    """
    if not str(closed_by or "").strip():
        return _reject("闭环必须由督办人员显式发起，closed_by 不能为空。")

    # 幂等：已闭环直接返回，不重复写
    existing = closure_store.get_closure(task_id, db_path=db_path)
    if existing is not None:
        return {"ok": True, "closed": True, "idempotent": True,
                "reason": "该任务已于此前正式闭环。", "closure": existing}

    task = sqlite_db.get_task(task_id, db_path=db_path)
    if task is None:
        raise sqlite_db.TaskNotFoundError(f"任务不存在：id={task_id}")

    # 防线 2：办结标准必须存在、已确认、来源合规
    meta = task_metadata_store.get_metadata(task_id, db_path=db_path)
    criteria = list(meta.get("closure_criteria") or [])
    source = meta.get("closure_source")
    if not criteria:
        return _reject("缺少办结标准，不能正式闭环。")
    if not meta.get("closure_confirmed"):
        return _reject("办结标准尚未经督办人员确认，不能作为强制闭环依据。")
    if source not in ALLOWED_CLOSURE_SOURCES:
        return _reject(f"办结标准来源为 {source!r}，不属于可闭环的合规来源。")

    # 防线 1：版本绑定 —— 必须是当前提交内容的 approved 核验
    submission = submission_store.get_submission(task_id, db_path=db_path)
    if submission is None:
        return _reject("尚无回执提交记录，不存在可闭环的办理结果。")
    current_digest = verification_store.content_digest(submission)
    if not current_digest:
        return _reject("提交内容为空，无法确定版本，不允许闭环。")

    records = verification_store.list_verifications(task_id, db_path=db_path)
    current = [r for r in records if r.get("submission_digest") == current_digest]
    if not current:
        return _reject("当前提交版本尚未核验，旧版本的核验结论不能用于新版本。")
    latest = current[-1]

    # 防线 3：当前版本不得存在未决结论
    unresolved = [r for r in current if r.get("status") in BLOCKING_STATUSES]
    if unresolved:
        return _reject(
            f"当前提交版本存在未解决结论（{unresolved[-1]['status']}），不能闭环。")

    if latest.get("status") != models.VERIFICATION_APPROVED:
        return _reject(f"当前提交版本核验结论为 {latest.get('status')}，未通过不能闭环。")

    # 防线 4：状态允许
    status = str(task.get("status") or "")
    if status in BLOCKED_TASK_STATUSES:
        return _reject(f"任务当前状态为「{status}」，属例外情形，不得按正常办结闭环。")

    # 高风险需显式授权
    if str(task.get("risk_level") or "") == HIGH_RISK and not str(high_risk_ack or "").strip():
        return _reject("高风险任务必须由督办人员显式授权并填写理由后才能闭环。")

    record = closure_store.close_task(
        task_id,
        int(submission["id"]),
        int(latest["id"]),
        submission_digest=current_digest,
        closed_by=closed_by,
        closure_note=closure_note,
        db_path=db_path,
    )
    sqlite_db.append_chain_log(
        task_id=int(task_id),
        action=models.ACTION_CLOSE,
        operator=closed_by,
        remark=(closure_note or "正式闭环")[:200],
        db_path=db_path,
    )
    return {"ok": True, "closed": True, "idempotent": False,
            "reason": "已正式闭环。", "closure": record}

def get_closure_status(task_id: int, db_path: Any = None) -> dict[str, Any]:
    """查询闭环状态，并给出闭环记录是否仍与当前提交一致。

    这是「界面该展示什么」的取数入口：闭环后若负责人又提交了新内容，
    stale=True 表示闭环依据已被新版本取代，历史记录保留但不代表当前版本已办结。
    """
    task = sqlite_db.get_task(task_id, db_path=db_path)
    if task is None:
        raise sqlite_db.TaskNotFoundError(f"任务不存在：id={task_id}")
    closure = closure_store.get_closure(task_id, db_path=db_path)
    if closure is None:
        return {"closed": False, "closure": None, "stale": False}
    submission = submission_store.get_submission(task_id, db_path=db_path)
    current = verification_store.content_digest(submission)
    return {
        "closed": True,
        "closure": closure,
        "stale": bool(current) and current != closure.get("submission_digest"),
    }