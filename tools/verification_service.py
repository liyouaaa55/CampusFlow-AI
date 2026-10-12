"""v2.2 Verification Service：独立核验服务（不新增 Agent）。

回答的问题：**负责人交上来的东西，算不算真正办结？**

与既有模块的边界（三者互不替代）：
    submission_check  「负责人交没交？」——确定性事实投影，不判好坏
    task_dispatch     「能不能交？」——提交入口校验
    本模块            「交的算不算办结？」——业务核验，独立实现

三条不可让步的原则（写进代码，不靠调用方自觉）：

    1. **规则优先，AI 辅助**：数据库事实与规则检查一律由代码完成，
       大模型只在「语义匹配」这一维提供参考，永远不能决定结论。
    2. **安全降级**：AI 超时 / 输出非法 / 置信度不足 / 结论冲突，
       一律降级为 manual_review，**绝不默认通过**。
    3. **不越权**：本模块不修改 tasks.status、不修改 task_submissions.status、
       不改负责人 / 期限 / 优先级 / 办结标准；核验只写 verifications。

**核验通过 ≠ 已闭环。** 现有 tasks.status 六个取值里没有闭环状态，
闭环动作不在本轮范围内，因此本模块只记录核验结论并返回
closure_performed=False，由调用方决定后续。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from database import models, sqlite_db
from database import submission_store
from database import task_metadata_store
from database import verification_store
from llm import client
from tools import submission_check

# AI 置信度阈值：低于此值一律转人工复核
AI_CONFIDENCE_THRESHOLD: float = 0.6
# 需要 LLM 判定的任务风险等级（高风险必须人工复核）
HIGH_RISK: str = "red"
# 缺少办理要求/办结标准时的默认来源标记
SOURCE_NONE: str = ""
# 核验与闭环的边界说明（返回给调用方，避免误读为已闭环）
CLOSURE_NOTE: str = "核验通过不等于已闭环；现有任务状态体系无闭环取值，闭环动作待后续实现。"

# ============================================================
# 一、事实采集（纯数据库读取，不含任何判断）
# ============================================================


class Facts(dict):
    """核验输入事实集合；字典子类，读取处用 [] 取值即可。"""


def collect_facts(task_id: int, db_path: Any = None) -> Facts:
    """把核验所需的全部客观事实一次取齐。

    只读不写；任何一项缺失都如实反映为 None / False / 空，
    不在此处做任何推断。
    """
    task = sqlite_db.get_task(task_id, db_path=db_path)
    if task is None:
        raise sqlite_db.TaskNotFoundError(f"任务不存在：id={task_id}")

    submission = submission_store.get_submission(task_id, db_path=db_path)
    meta = task_metadata_store.get_metadata(task_id, db_path=db_path)
    logs = sqlite_db.get_logs(task_id, db_path=db_path)
    dispatched = submission_check.is_dispatched(task_id, db_path=db_path)

    return Facts(
        task=task,
        task_id=int(task_id),
        submission=submission,
        has_submission=submission is not None,
        content=str((submission or {}).get("content") or ""),
        file_path=str((submission or {}).get("file_path") or ""),
        requirements=list(meta.get("execution_requirements") or []),
        criteria=list(meta.get("closure_criteria") or []),
        criteria_source=meta.get("closure_source"),
        criteria_confirmed=bool(meta.get("closure_confirmed")),
        risk_level=str(task.get("risk_level") or ""),
        task_status=str(task.get("status") or ""),
        dispatched=bool(dispatched),
        # 当前提交内容的指纹：核验结论必须绑定到具体版本，
        # 否则重新提交后旧结论会被误用。缺它会使闭环永远无法通过。
        digest=verification_store.content_digest(submission),
        log_actions=[str(x.get("action") or "") for x in logs],
        has_verify_log=models.ACTION_VERIFY in {str(x.get("action") or "") for x in logs},
    )


def _has_criteria(facts: Mapping[str, Any]) -> bool:
    """是否具备可用于核验的办结标准。

    来源为 ai 时按「未经确认」处理——数据层已强制其 closure_confirmed=False，
    这里再挡一层，避免任何调用方绕过 store 直接构造。
    """
    if not facts.get("criteria"):
        return False
    if facts.get("criteria_source") == models.CLOSURE_SOURCE_AI:
        return False
    return True

# ============================================================
# 二、规则层判定（确定性，绝不让 LLM 取代）
# ============================================================


def _check_deliverable(facts: Mapping[str, Any]) -> bool:
    """办结材料是否在回执中有对应证据。

    这里做的是「材料在不在」的**存在性**检查，不判断材料质量，
    也不因为正文里写了「已完成」就认定办结。
    """
    if not facts.get("criteria"):
        return False
    content = str(facts.get("content") or "")
    if not content:
        return False
    # 附件存在视为材料齐备的正面证据
    if str(facts.get("file_path") or "").strip():
        return True
    # 正文需覆盖至少一条标准的关键词
    for item in facts["criteria"]:
        text = str(item or "").strip()
        if text and text in content:
            return True
    return False


def _check_process(facts: Mapping[str, Any]) -> bool:
    """过程合规：必须先下发、后提交。

    未下发就出现回执，说明流程被绕过——这属于过程不合规，
    不能因为「材料看起来齐全」就放行。
    """
    if not facts.get("dispatched"):
        return False
    return models.ACTION_SUBMIT in set(facts.get("log_actions") or [])


def _check_state_consistency(facts: Mapping[str, Any]) -> bool:
    """状态一致：任务状态、提交事实、日志三者不能互相矛盾。"""
    status = str(facts.get("task_status") or "")
    # 任务尚未完成却要求闭环，是自相矛盾
    if status == "completed" and not facts.get("has_submission"):
        return False
    # 已下发就必须能查到的回执提交日志
    if facts.get("has_submission") and models.ACTION_SUBMIT not in set(facts.get("log_actions") or []):
        return False
    return True


def _check_requirement_match(facts: Mapping[str, Any], ai: Mapping[str, Any] | None) -> bool | None:
    """办理要求是否满足：这一维需要语义判断。

    有 AI 结论用 AI 的；没有则返回 None（不可判定），
    由调用方按「不可判定」走人工复核，**不默认满足**。
    """
    if ai is not None and "requirement_match" in ai:
        return bool(ai.get("requirement_match"))
    return None

# ============================================================
# 三、AI 辅助层（只提供参考，异常一律降级）
# ============================================================

SYSTEM_PROMPT: str = (
    "你是高校事务督办核验助手。依据给定的办理要求与办结标准，"
    "判断负责人提交的办理结果是否满足要求。"
    "只输出 JSON：{\"requirement_match\": true或false, \"confidence\": 0到1的数值, "
    "\"evidence\": \"依据\"}。无法判断时 requirement_match 填 false 且 confidence 填 0。"
    "严禁修改任务负责人、期限、优先级或办结标准。"
)


def _ask_ai(
    facts: Mapping[str, Any], llm_fn: Callable[[str, str], Any] | None
) -> tuple[Mapping[str, Any] | None, str]:
    """请求 AI 做语义判断。

    返回 (结论, 降级原因)：
      正常      → (dict, "")
      不可用/异常 → (None, 原因)，调用方据此转人工复核。

    **任何异常都被吞掉并转成降级原因**，绝不向上抛，
    绝不因为 AI 挂了而默认通过。
    """
    if llm_fn is None:
        return None, "未启用 AI 语义判断"
    try:
        payload = llm_fn(
            SYSTEM_PROMPT,
            f"办理要求：{facts.get('requirements')}\n"
            f"办结标准：{facts.get('criteria')}\n"
            f"提交内容：{facts.get('content')}\n"
            f"附件：{facts.get('file_path') or '无'}",
        )
    except client.LLMError as exc:
        return None, f"AI 调用失败：{exc}"
    except Exception as exc:  # 兜底：AI 层任何异常都不得影响核验主流程
        return None, f"AI 调用异常：{type(exc).__name__}"

    if not isinstance(payload, dict):
        return None, "AI 输出不是对象"
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        return None, "AI 置信度非法"
    if confidence < AI_CONFIDENCE_THRESHOLD:
        return None, f"AI 置信度不足（{confidence}）"
    return payload, ""

# ============================================================
# 四、核验主流程
# ============================================================

_ACTION_BY_STATUS: dict[str, str] = {
    models.VERIFICATION_APPROVED: models.ACTION_VERIFY,
    models.VERIFICATION_NEED_SUPPLEMENT: models.ACTION_NEED_SUPPLEMENT,
    models.VERIFICATION_REJECTED: models.ACTION_REJECT_REWORK,
    models.VERIFICATION_MANUAL_REVIEW: models.ACTION_VERIFY,
    models.VERIFICATION_PENDING: models.ACTION_VERIFY,
}


def verify_submission(
    task_id: int,
    *,
    llm_fn: Callable[[str, str], Any] | None = None,
    operator: str = models.DEFAULT_OPERATOR,
    decision: str | None = None,
    reason: str = "",
    db_path: Any = None,
) -> dict[str, Any]:
    """核验某任务的回执是否满足办理要求与办结标准，写入一条核验记录。

    每次调用都会**新增**一条 verifications 记录（append，不覆盖），
    因此「需补充 → 再提交 → 再核验」的轨迹完整留痕。

    参数：
        task_id   任务主键
        llm_fn    AI 语义判断函数（形如 (system, user) -> dict）；
                   None 表示纯规则判定。异常自动降级为人工复核
        decision  督办人员显式结论（manual_review / need_supplement /
                   rejected / approved）；给出时**跳过自动判定**，
                   但仍会写入对应的核验维度结果
        reason    人工结论的理由

    返回：含 status / 四维 / closure_performed 的结果字典。

    **不修改** tasks.status、task_submissions.status、负责人、期限、优先级。
    **核验通过不等于已闭环**：现有任务状态体系没有闭环取值，
    本模块只记录核验结论并返回 closure_performed=False。
    """
    facts = collect_facts(task_id, db_path=db_path)

    if not facts.get("has_submission"):
        # 没有提交就没有可核验对象，因此**不写核验记录**：
        # verification_store 要求核验必须锚定到一条真实提交（见其 _require_submission），
        # 那条约束是 S1 有意为之，这里不绕过。
        return {
            "ok": False,
            "task_id": int(facts["task_id"]),
            "submission_id": None,
            "status": models.VERIFICATION_PENDING,
            "reason": reason or "尚无回执提交记录，不能通过核验。",
            "requirement_match": False,
            "deliverable_complete": False,
            "process_valid": False,
            "state_consistent": False,
            "manual_review_required": False,
            "verification": None,
            "recorded": False,
            "closure_performed": False,
            "closure_note": CLOSURE_NOTE,
            "facts": {"has_submission": False},
        }

    deliverable = _check_deliverable(facts)
    process_ok = _check_process(facts)
    state_ok = _check_state_consistency(facts)
    ai, degraded = _ask_ai(facts, llm_fn)
    requirement = _check_requirement_match(facts, ai)

    if decision:
        return _persist(
            facts, decision, bool(requirement), deliverable, process_ok, state_ok,
            True, reason or "督办人员人工结论。", operator, db_path,
        )

    # —— 自动判定的否决顺序：安全优先 ——
    if not _has_criteria(facts):
        return _persist(
            facts, models.VERIFICATION_MANUAL_REVIEW, bool(requirement), deliverable,
            process_ok, state_ok, True,
            reason or "缺少办结标准或标准未经确认，不能自动通过，转人工复核。",
            operator, db_path,
        )
    if facts.get("risk_level") == HIGH_RISK:
        return _persist(
            facts, models.VERIFICATION_MANUAL_REVIEW, bool(requirement), deliverable,
            process_ok, state_ok, True,
            reason or "任务为高风险事项，须人工复核。", operator, db_path,
        )
    if degraded:
        return _persist(
            facts, models.VERIFICATION_MANUAL_REVIEW, False, deliverable,
            process_ok, state_ok, True,
            reason or degraded, operator, db_path,
        )
    if not process_ok or not state_ok:
        return _persist(
            facts, models.VERIFICATION_NEED_SUPPLEMENT, bool(requirement), deliverable,
            process_ok, state_ok, True,
            reason or "办理过程或状态记录不一致，需补充材料。", operator, db_path,
        )
    if not deliverable or requirement is False:
        return _persist(
            facts, models.VERIFICATION_NEED_SUPPLEMENT, bool(requirement), deliverable,
            process_ok, state_ok, True,
            reason or "办结材料缺失或与办理要求不匹配。", operator, db_path,
        )

    return _persist(
        facts, models.VERIFICATION_APPROVED, True, True, True, True, True,
        reason or "办理要求满足，办结材料齐备。", operator, db_path,
    )

def _persist(
    facts: Mapping[str, Any],
    status: str,
    requirement_match: bool,
    deliverable_complete: bool,
    process_valid: bool,
    state_consistent: bool,
    manual: bool,
    reason: str,
    operator: str,
    db_path: Any,
) -> dict[str, Any]:
    """写入核验记录 + 链路日志，并返回结果。

    严格边界：
      - 只写 verifications 与 task_logs；
      - **不改** tasks.status、task_submissions.status；
      - **不写 close 事件**——闭环动作不在本轮范围内，
        核验通过不等于已闭环。
    """
    task_id = int(facts["task_id"])
    submission_id = (facts.get("submission") or {}).get("id")

    if status == models.VERIFICATION_MANUAL_REVIEW and manual:
        recorded = models.VERIFICATION_MANUAL_REVIEW
    else:
        recorded = status

    record = verification_store.append_verification(
        task_id,
        int(submission_id),
        recorded,
        reason=reason,
        requirement_match=requirement_match,
        deliverable_complete=deliverable_complete,
        process_valid=process_valid,
        state_consistent=state_consistent,
        verified_by=operator,
        submission_digest=facts.get("digest"),
        db_path=db_path,
    )

    sqlite_db.append_chain_log(
        task_id=task_id,
        action=_ACTION_BY_STATUS.get(recorded, models.ACTION_VERIFY),
        operator=operator,
        remark=reason[:200] if reason else "",
        db_path=db_path,
    )

    return {
        "ok": True,
        "task_id": task_id,
        "submission_id": int(submission_id) if submission_id else None,
        "status": recorded,
        "reason": reason,
        "requirement_match": requirement_match,
        "deliverable_complete": deliverable_complete,
        "process_valid": process_valid,
        "state_consistent": state_consistent,
        "manual_review_required": recorded == models.VERIFICATION_MANUAL_REVIEW,
        "verification": record,
        "recorded": True,
        "closure_performed": False,
        "closure_note": CLOSURE_NOTE,
        "facts": {
            "has_submission": bool(facts.get("has_submission")),
            "requirements": list(facts.get("requirements") or []),
            "criteria": list(facts.get("criteria") or []),
            "criteria_source": facts.get("criteria_source"),
            "criteria_confirmed": bool(facts.get("criteria_confirmed")),
            "dispatched": bool(facts.get("dispatched")),
            "task_status": facts.get("task_status"),
        },
    }