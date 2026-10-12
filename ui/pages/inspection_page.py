"""页面五：执行检查（办理结果与办结标准 → 核验与正式闭环）。

职责边界（严格）：
    - 本页只做「渲染 + 编排」；
    - 核验结论由 tools.verification_service 产出，本页不自行判断；
    - 闭环由 tools.closure_service 执行，本页不直接写库；
    - **绝不修改 tasks.status 来伪造闭环**——闭环是 task_closures 表的独立事实。

三条不可混淆的状态：
    已提交（task_submissions） ≠ 已核验（verifications） ≠ 已闭环（task_closures）

页面信息架构（v3.0 演示收敛，仅调整展示层级）：
    顶部      ：一张紧凑任务卡（任务名称 / 责任部门 / 负责人 / 截止时间 / 当前阶段）
    01 执行反馈：页面主操作区（状态 / 说明 / 附件 / 录入人 / 保存，全部可见）
    02 持续督办：风险摘要 → 当前最需要处理的一项 → 完整明细（默认折叠）
    01 办理结果与办结标准：标准摘要与确认状态可见，明细与编辑入口折叠
    02 核验与正式闭环：当前版本结论 → 发起核验 → 历史（折叠）→ 闭环前置条件

一致性纪律（本轮强调）：
    1. 一切「当前核验结论」都以「当前有效提交版本」的数据库记录为准，
       绝不使用 session_state 里上一次操作的结果冒充当前状态；
    2. 核验历史显式标注「当前提交版本 / 历史版本」，旧版本通过不得被读成当前通过；
    3. 闭环前置条件与 tools.closure_service.close_task 使用同一套规则常量
       （BLOCKING_STATUSES / BLOCKED_TASK_STATUSES / ALLOWED_CLOSURE_SOURCES），
       页面只做只读预告，真正的闸门始终在后端。
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any, Mapping, Sequence

import streamlit as st

from database import (
    closure_store,
    feedback_store,
    models,
    sqlite_db,
    submission_store,
    task_metadata_store,
    verification_store,
)
from llm import client
from tools import closure_service as closure_svc
from tools import execution_check
from tools import submission_check
from tools import verification_service as verify_svc
from ui import labels
from ui.components import cards, state, styles, tables
from ui.pages import execution_panel, submission_view

PAGE_TITLE: str = "执行反馈与检查"
PAGE_SUBTITLE: str = "录入执行反馈与附件、同步任务状态、持续督办，并核验办理结果、办理正式闭环"

# --- 顶部紧凑信息卡 ---
TASK_CARD_TITLE: str = "当前任务"
LBL_DEPT: str = "责任部门"
LBL_OWNER: str = "负责人"
LBL_DEADLINE: str = "截止时间"

# --- 两个核心功能区 ---
SECTION_RESULT: str = "01　办理结果与办结标准"
CAPTION_RESULT: str = "办结标准是闭环依据；负责人提交的办理结果在同一区查看。标准与结果不是同一业务状态。"
SECTION_VERIFY_CLOSE: str = "02　AI 核验与正式闭环"
CAPTION_VERIFY_CLOSE: str = (
    "核验结论只对「当前有效提交版本」有效；核验通过且全部前置条件满足后方可正式闭环。"
)

# --- 功能区内部小标题 ---
SUB_CRITERIA: str = "办结标准"
SUB_RESULT: str = "负责人提交的办理结果"
SUB_CONCLUSION: str = "当前有效提交的核验结论"
SUB_VERIFY_ACTION: str = "发起核验"
SUB_HISTORY: str = "核验历史"
SUB_CLOSURE: str = "正式闭环"
SUB_BLOCKERS: str = "闭环前置条件"
EXPANDER_CRITERIA: str = "编辑办结标准与标准来源"

# --- 当前阶段（由真实业务数据派生，不依赖按钮点击 / session_state）---
STAGE_TITLE: str = "当前阶段"
STAGE_CAPTION: str = "由任务、回执、核验与闭环数据派生"
STAGE_LABELS: dict[str, str] = {
    "pending_submit": "待提交回执",
    "pending_verify": "待核验",
    "not_passed": "核验未通过，需补充或重办",
    "ready_close": "核验通过，待正式闭环",
    "closed": "已正式闭环",
}

BTN_VERIFY: str = "发起核验"
BTN_CLOSE: str = "正式闭环"
BTN_SAVE_CRITERIA: str = "保存并确认"

LBL_OPERATOR: str = "核验人"
LBL_DECISION: str = "人工裁决"
LBL_REASON: str = "核验说明"
LBL_NOTE: str = "闭环说明"
LBL_ACK: str = "高风险授权理由"
LBL_CLOSER: str = "闭环人"
LBL_CRITERIA: str = "办结标准条目"
LBL_SOURCE: str = "标准来源"

DIMENSION_KEYS: tuple[str, ...] = labels.VERIFICATION_DIMENSIONS

# 高风险取值直接取自 RISK_LEVELS 末位，避免在页面硬编码字符串
RISK_HIGH_VALUE: str = models.RISK_LEVELS[-1]

# 人工裁决可选值：留空表示交由系统按事实判定
DECISION_OPTIONS: dict[str, str] = {
    "按事实自动判定": "",
    "人工判定通过": models.VERIFICATION_APPROVED,
    "要求补充材料": models.VERIFICATION_NEED_SUPPLEMENT,
    "驳回重办": models.VERIFICATION_REJECTED,
    "转人工复核": models.VERIFICATION_MANUAL_REVIEW,
}

# 标准来源下拉：{界面文案: 数据层枚举}
CRITERIA_SOURCE_OPTIONS: dict[str, str] = {
    "材料明确": models.CLOSURE_SOURCE_MATERIAL,
    "规则推导": models.CLOSURE_SOURCE_RULE,
    "人工确认": models.CLOSURE_SOURCE_MANUAL,
}

# 核验不通过时给负责人的处理指引（不伪造系统不存在的流程）
GUIDANCE_NEED_SUPPLEMENT: str = (
    "请负责人在「提交执行结果」处重新提交补充材料；重新提交后旧核验结论自动失效，需重新发起核验。"
)
GUIDANCE_REJECT_REWORK: str = (
    "本次结论为驳回重办。请负责人按驳回意见重新办理并重新提交，系统将按新内容重新核验。"
)
GUIDANCE_MANUAL_REVIEW: str = (
    "本条已转人工复核，需由督办人员查看材料后人工裁决，系统不做自动判定。"
)
GUIDANCE_PENDING: str = "核验未通过，请按上方原因补充或更正后重新提交。"

CLOSED_HINT: str = "已闭环任务不可重复办理闭环；如需更正请走线下流程。"
CLOSE_BUTTON_DISABLED_HINT: str = "前置条件未满足，闭环按钮不可用。"

# ============================================================
# v3.0 第二阶段：执行反馈与持续督办（第四页核心区域）
# ============================================================

# --- 模块一：任务信息与执行状态 ---
SECTION_TASK_CONTEXT: str = "任务信息与执行状态"
CAPTION_TASK_CONTEXT: str = "先确认当前任务与所处阶段；下方反馈、督办、核验与闭环均针对同一个任务"

# --- 模块二：执行反馈与 AI 持续督办 ---
SECTION_EXEC_SUPERVISION: str = "执行反馈与 AI 持续督办"
CAPTION_EXEC_SUPERVISION: str = (
    "督办结论来自真实任务状态、截止时间、最新有效反馈与督办记录，不使用模型推测进展；"
    "已正式闭环的任务不参与督办"
)
# 反馈表单默认按需展开：无反馈时展开（便于首次录入），已有反馈时折叠（先看摘要）
EXPANDER_FEEDBACK_FORM: str = "填写或补充执行反馈"
FOCUS_TITLE: str = "当前最需要处理"
FOCUS_OK: str = "当前任务未进入督办清单：任务状态、截止时间与最新反馈均未触发督办规则。"
FOCUS_CLOSED: str = "该任务已正式闭环，不再参与督办。"
FOCUS_OTHERS: str = "全局另有 {count} 项任务需要关注，可在下方「辅助信息」中查看。"

# --- 辅助区：全局统计与其他风险任务（默认折叠）---
SECTION_AUX: str = "辅助信息：全局督办统计与其他风险任务"
CAPTION_AUX: str = "不影响当前任务的判断；展开后可查看全局统计与其他风险任务的依据与建议"
AUX_KPI_TITLE: str = "全局督办风险统计"
AUX_DETAIL_TITLE: str = "其他风险任务明细"

# --- 模块三内部：折叠的闭环表单 ---
EXPANDER_CLOSE_FORM: str = "填写闭环说明并办理正式闭环"

# 摘要分组：标签 / 说明 / 颜色 / 归属督办类型。
# 类型互斥（_classify 只返回一个），因此一个任务只会被计入一组，不重复计数。
SUMMARY_GROUPS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "已下发未反馈",
        "已下发但尚未收到执行反馈",
        styles.PRIMARY,
        (execution_check.SUPERVISION_NOT_FEEDBACK,),
    ),
    (
        "临期未完成",
        "临近截止时间且仍未完成",
        styles.WARNING,
        (execution_check.SUPERVISION_DUE_SOON,),
    ),
    (
        "逾期未完成",
        "已超过截止时间且仍未完成",
        styles.DANGER,
        (execution_check.SUPERVISION_OVERDUE,),
    ),
    (
        "其他需关注",
        "已反馈仍逾期、长期无实质进展",
        styles.TEXT_MUTED,
        (
            execution_check.SUPERVISION_FEEDBACK_UNFINISHED,
            execution_check.SUPERVISION_STALE,
        ),
    ),
)

BTN_SAVE_FEEDBACK: str = "保存执行反馈"
LBL_FEEDBACK_STATUS: str = "反馈状态"
LBL_FEEDBACK_NOTE: str = "工作说明（选填）"
LBL_FEEDBACK_ATTACH: str = "附件（选填）"
LBL_FEEDBACK_OPERATOR: str = "录入人"

# 反馈状态下拉：界面中文 → 内部枚举
FEEDBACK_OPTIONS: dict[str, str] = {
    "已完成": models.FEEDBACK_COMPLETED,
    "进行中／暂未完成": models.FEEDBACK_IN_PROGRESS,
}

HINT_NO_FEEDBACK: str = "该任务尚未录入反馈（当前记为「未反馈」）。"
HINT_FEEDBACK_STATUS_RULE: str = (
    "选择「已完成」会把任务状态更新为已完成；"
    "选择「进行中／暂未完成」不会把任务标记为已完成。"
)
HINT_FEEDBACK_NOT_CLOSURE: str = (
    "已完成不等于正式闭环：正式闭环仍需核验通过并满足闭环全部前置条件。"
)
HINT_FEEDBACK_NOT_DISPATCHED: str = (
    "该任务尚未在系统内下发，不能录入执行反馈。请先到「任务督办中心」完成系统内下发。"
)
HINT_NO_SUPERVISION: str = "当前没有需要持续督办的任务。"

MSG_FEEDBACK_FAILED: str = "执行反馈保存失败，请稍后重试。"

# 查看范围选项（保留既有的「待办理 / 已闭环 / 全部」，新增按执行阶段区分）
SCOPE_PENDING: str = "待办理"
SCOPE_DISPATCHED: str = "已下发"
SCOPE_NOT_DISPATCHED: str = "未下发"
SCOPE_COMPLETED: str = "已完成"
SCOPE_CLOSED: str = "已闭环"
SCOPE_ALL: str = "全部"
SCOPE_OPTIONS: tuple[str, ...] = (
    SCOPE_PENDING,
    SCOPE_DISPATCHED,
    SCOPE_NOT_DISPATCHED,
    SCOPE_COMPLETED,
    SCOPE_CLOSED,
    SCOPE_ALL,
)


# ============================================================
# 业务动作（不依赖 Streamlit 运行时，便于直接单测）
# ============================================================


def run_verification(
    task_id: int,
    *,
    operator: str = models.DEFAULT_OPERATOR,
    decision: str | None = None,
    reason: str = "",
    llm_fn: Any = None,
    db_path: Any = None,
) -> dict[str, Any]:
    """发起核验，返回统一结构。

    异常一律转成中文提示，**绝不向界面暴露 traceback**。
    「已提交」不等于「已核验」：无提交记录时 service 会返回 ok=False，
    这里如实展示，不做任何补造。
    """
    try:
        # 与 task_agent 保持一致：未显式注入时走真实模型客户端。
        # 若传 None，verification_service._ask_ai 会判定「未启用 AI 语义判断」，
        # 结果恒为 manual_review，页面将永远无法自动核验通过。
        result = verify_svc.verify_submission(
            task_id,
            llm_fn=llm_fn or client.chat_json,
            operator=operator,
            decision=decision or None,
            reason=reason,
            db_path=db_path,
        )
    except sqlite_db.DatabaseError as exc:
        return {"ok": False, "kind": "db_error", "message": f"核验失败：{exc}", "result": None}
    except Exception:
        return {"ok": False, "kind": "unexpected", "message": "结果核验失败，请稍后重试。", "result": None}
    return {"ok": True, "kind": "verified", "message": "", "result": result}


def run_closure(
    task_id: int,
    *,
    closed_by: str,
    closure_note: str = "",
    high_risk_ack: str = "",
    db_path: Any = None,
) -> dict[str, Any]:
    """执行正式闭环，返回统一结构。

    幂等由 closure_service 保证：重复调用返回 idempotent=True，不产生第二条记录。
    """
    try:
        result = closure_svc.close_task(
            task_id,
            closed_by=closed_by,
            closure_note=closure_note,
            high_risk_ack=high_risk_ack,
            db_path=db_path,
        )
    except sqlite_db.TaskNotFoundError as exc:
        return {"ok": False, "kind": "not_found", "message": str(exc), "result": None}
    except sqlite_db.DatabaseError as exc:
        return {"ok": False, "kind": "db_error", "message": f"闭环失败：{exc}", "result": None}
    except Exception:
        return {"ok": False, "kind": "unexpected", "message": "正式闭环失败，请稍后重试。", "result": None}
    return {"ok": True, "kind": "closed", "message": "", "result": result}


def run_save_feedback(
    task_id: int,
    *,
    status: str,
    note: str = "",
    file_path: Any = None,
    operator: str = models.DEFAULT_OPERATOR,
    db_path: Any = None,
) -> dict[str, Any]:
    """保存执行反馈（追加留痕 + 同步任务状态），返回统一结构。

    与核验/闭环的关系：反馈只改「任务执行状态」，**不产生核验结论、不闭环**；
    已完成 ≠ 核验通过 ≠ 正式闭环，三者仍严格分离。

    异常一律转成中文提示，绝不向界面暴露 traceback。
    """
    try:
        result = execution_check.save_feedback(
            task_id,
            status=status,
            note=note,
            file_path=file_path,
            operator=operator,
            db_path=db_path,
        )
    except sqlite_db.TaskNotFoundError:
        return {"ok": False, "kind": "not_found", "message": "任务不存在，保存已取消。"}
    except feedback_store.FeedbackError as exc:
        return {"ok": False, "kind": "invalid", "message": f"保存失败：{exc}"}
    except sqlite_db.DatabaseError:
        return {"ok": False, "kind": "db_error", "message": "保存失败：数据库繁忙，请稍后重试。"}
    except Exception:
        return {"ok": False, "kind": "unexpected", "message": MSG_FEEDBACK_FAILED}

    if not result.get("ok"):
        return {
            "ok": False,
            "kind": "rejected",
            "message": str(result.get("reason") or "保存被拒绝。"),
        }

    applied = result.get("status_applied")
    if applied == "completed":
        message = "执行反馈已保存，任务状态已更新为「已完成」。"
    elif applied == "processing":
        message = "执行反馈已保存，任务状态已更正为「进行中」。"
    else:
        message = "执行反馈已保存（任务状态无需变更）。"
    return {
        "ok": True,
        "kind": "saved",
        "message": message,
        "status_applied": applied,
        "feedback": result.get("feedback"),
    }


# ============================================================
# 只读事实派生（当前有效提交 / 当前阶段 / 闭环前置条件）
# ============================================================


def _current_verification(task_id: int) -> dict[str, Any] | None:
    """取「当前提交版本」（submission_digest 匹配）的最近一条核验；无则 None。

    口径与 closure_service.close_task 严格一致：
    旧提交版本的核验结论不能用于新版本，避免「历史通过」冒充「当前通过」。
    """
    submission = submission_store.get_submission(task_id)
    if not submission:
        return None
    digest = verification_store.content_digest(submission)
    if not digest:
        return None
    current = [
        r
        for r in verification_store.list_verifications(task_id)
        if r.get("submission_digest") == digest
    ]
    return current[-1] if current else None


def _current_stage(task_id: int) -> str:
    """按真实业务数据派生当前办理阶段（只读，不依赖 session_state）。"""
    if closure_store.get_closure(task_id):
        return "closed"
    if not submission_store.get_submission(task_id):
        return "pending_submit"
    current = _current_verification(task_id)
    if not current:
        # 有提交、但当前版本尚无核验（旧版本结论不算）
        return "pending_verify"
    if str(current.get("status")) == models.VERIFICATION_APPROVED:
        return "ready_close"
    return "not_passed"


def _closure_blockers(task: Mapping[str, Any], task_id: int) -> list[str]:
    """只读预告「正式闭环」当前不满足的前置条件。

    为什么要单独做这一层：闭环按钮可用性必须与后端闸门一致，否则会出现
    「页面显示可闭环、点击后被后端拒绝」的矛盾观感。规则**取值**一律取自
    tools.closure_service 的公开常量，页面不另立一套阈值：

        - 防线 2 办结标准：非空 + 已确认 + 来源在 ALLOWED_CLOSURE_SOURCES；
        - 防线 1 版本绑定：approved 必须属于当前提交内容（digest 一致）；
        - 防线 3 无未决结论：当前版本不得存在 BLOCKING_STATUSES；
        - 防线 4 状态允许：任务不得处于 BLOCKED_TASK_STATUSES；
        - 高风险：必须显式授权理由。

    与 close_task 的唯一差别是「全部列出」而不是「返回第一条」，
    这样界面能一次说清所有阻断原因；最终闸门仍在 close_task，未被削弱。
    """
    blockers: list[str] = []

    # ---- 防线 2：办结标准 ----
    meta = task_metadata_store.get_metadata(task_id)
    criteria = list(meta.get("closure_criteria") or [])
    source = meta.get("closure_source")
    if not criteria:
        blockers.append("缺少办结标准，不能正式闭环。")
    else:
        if not meta.get("closure_confirmed"):
            blockers.append("办结标准尚未经督办人员确认，不能作为强制闭环依据。")
        if source not in closure_svc.ALLOWED_CLOSURE_SOURCES:
            blockers.append(
                f"办结标准来源为「{labels.closure_source_label(source)}」，不属于可闭环的合规来源。"
            )

    # ---- 防线 1 + 3：当前提交版本的核验结论 ----
    submission = submission_store.get_submission(task_id)
    if submission is None:
        blockers.append("尚无回执提交记录，不存在可闭环的办理结果。")
    else:
        digest = verification_store.content_digest(submission)
        if not digest:
            blockers.append("提交内容为空，无法确定版本，不允许闭环。")
        else:
            records = [
                r
                for r in verification_store.list_verifications(task_id)
                if r.get("submission_digest") == digest
            ]
            if not records:
                blockers.append(
                    "当前提交版本尚未核验（旧版本结论不算），请重新发起核验后再办理闭环。"
                )
            else:
                unresolved = [
                    r for r in records if r.get("status") in closure_svc.BLOCKING_STATUSES
                ]
                latest = records[-1]
                if unresolved:
                    blockers.append(
                        "当前提交版本存在未解决结论"
                        f"（{labels.verification_label(unresolved[-1].get('status'))}），不能闭环。"
                    )
                elif str(latest.get("status")) != models.VERIFICATION_APPROVED:
                    blockers.append(
                        f"当前提交版本核验结论为{labels.verification_label(latest.get('status'))}，"
                        "未通过不能闭环。"
                    )

    # ---- 防线 4：任务状态 ----
    task_status = str(task.get("status") or "")
    if task_status in closure_svc.BLOCKED_TASK_STATUSES:
        blockers.append(
            f"任务当前状态为「{labels.status_label(task_status)}」，属例外情形，不得按正常办结闭环。"
        )

    # ---- 高风险需显式授权 ----
    if str(task.get("risk_level") or "") == RISK_HIGH_VALUE:
        blockers.append("高风险任务必须由督办人员显式授权并填写理由。")

    return blockers


# ============================================================
# 区域 A / B：执行反馈（录入 + 轨迹）
# ============================================================


def _render_feedback_summary(task: Mapping[str, Any], task_id: int) -> tuple[bool, bool]:
    """执行反馈摘要（模块二首屏）。

    只展示真实数据：当前有效反馈状态、说明、提交时间、附件与录入人；
    没有反馈时明确显示「尚未录入反馈」，不虚构任何提交记录。

    返回 (是否允许录入/补充反馈, 是否已有反馈)：
        - 已正式闭环 → (False, ...) 并给出闭环保护提示；
        - 未下发 → (False, False) 并给出未下发提示。
    """
    if closure_store.is_closed(int(task_id)):
        st.success(execution_check.CLOSED_REASON)
        return False, False

    if not submission_check.is_dispatched(int(task_id)):
        cards.hint([HINT_FEEDBACK_NOT_DISPATCHED])
        return False, False

    latest = feedback_store.latest_feedback(int(task_id))
    has_feedback = latest is not None
    if latest is None:
        cards.hint([HINT_NO_FEEDBACK])
    else:
        st.markdown(
            f"**当前有效反馈：{labels.feedback_label(latest.get('status'))}**"
        )
        note = str(latest.get("note") or "").strip()
        if note:
            st.write(note)
        attachment = str(latest.get("file_path") or "").strip()
        st.caption(
            f"反馈时间：{latest.get('created_at')}　｜　"
            f"录入人：{latest.get('operator') or labels.UNKNOWN_TEXT}"
            f"　｜　附件：{attachment or '无'}"
        )

    history = feedback_store.list_feedback(int(task_id))
    if len(history) > 1:
        with st.expander(f"反馈轨迹（共 {len(history)} 条）", expanded=False):
            for record in reversed(history):
                st.markdown(
                    f"- {record.get('created_at')}　{labels.feedback_label(record.get('status'))}"
                    f"　（录入人：{record.get('operator') or labels.UNKNOWN_TEXT}）"
                )
                if record.get("note"):
                    st.caption(str(record["note"]))
    return True, has_feedback


def _render_feedback_form(task_id: int, *, has_feedback: bool) -> None:
    """执行反馈录入表单（按需展开，提交能力与规则完全保留）。

    默认展开状态：无反馈时展开（首次录入一步到位）；已有反馈时折叠（先看摘要）。
    三条硬约束不变：
        1. 反馈与当前选中的 task_id 严格绑定（表单 key 含 task_id）；
        2. 附件复用既有安全落盘能力（大小校验 + 只取文件名），绑定到本次反馈；
        3. 已正式闭环 / 未下发任务不提供录入入口（由调用方判定后决定是否调用）。
    """
    with st.expander(EXPANDER_FEEDBACK_FORM, expanded=not has_feedback):
        st.caption(HINT_FEEDBACK_STATUS_RULE)
        with st.form(key=f"cf_feedback_form_{task_id}"):
            status_label = st.radio(
                LBL_FEEDBACK_STATUS, list(FEEDBACK_OPTIONS), horizontal=True
            )
            note = st.text_area(
                LBL_FEEDBACK_NOTE, height=80, key=f"cf_feedback_note_{task_id}"
            )
            uploaded = st.file_uploader(
                LBL_FEEDBACK_ATTACH,
                type=list(submission_view.ALLOWED_ATTACH_TYPES),
                key=f"cf_feedback_attach_{task_id}",
                help=(
                    "附件为可选项，例如现场照片、执行台账、成果文件或阶段性进度说明，"
                    "与本次反馈一起绑定到当前任务。是否完成任务以反馈状态为准，"
                    "上传附件不代表任务已完成。"
                ),
            )
            operator = st.text_input(
                LBL_FEEDBACK_OPERATOR,
                value=models.DEFAULT_OPERATOR,
                key=f"cf_feedback_op_{task_id}",
            )
            submitted = st.form_submit_button(BTN_SAVE_FEEDBACK, type="primary")

        st.caption(HINT_FEEDBACK_NOT_CLOSURE)

        if not submitted:
            return

        attachment_path = submission_view.save_attachment(uploaded)
        outcome = run_save_feedback(
            task_id,
            status=FEEDBACK_OPTIONS[status_label],
            note=note,
            file_path=attachment_path,
            operator=operator.strip() or models.DEFAULT_OPERATOR,
        )
        if outcome["ok"]:
            state.set_flash(outcome["message"])
            st.rerun()
        else:
            st.error(outcome["message"])


# ============================================================
# 区域 C：执行期间持续督办
# ============================================================


def _render_exec_supervision_block(task: Mapping[str, Any], task_id: int) -> None:
    """模块二：执行反馈与 AI 持续督办。

    展示顺序（判定全部复用 tools.execution_check，不另造数据）：
        1. 当前执行反馈摘要（真实记录，无反馈时明确显示未反馈）；
        2. AI 持续督办结论（针对当前任务：风险类型 / 判断依据 / 建议下一步）；
        3. 按需展开的反馈录入表单与执行期处置入口；
        4. 全局统计与其他风险任务放入「辅助信息」折叠区。
    """
    cards.section(SECTION_EXEC_SUPERVISION, CAPTION_EXEC_SUPERVISION)

    can_edit, has_feedback = _render_feedback_summary(task, task_id)

    items = execution_check.supervision_items(today=date.today())
    _render_supervision_focus(items, task_id)

    if can_edit:
        _render_feedback_form(task_id, has_feedback=has_feedback)

    st.write("")
    # 执行期处置（v2.0 受控动作链路）：调用独立的执行期督办模块，
    # 与第三页的下发前流程互不依赖；该面板自身默认折叠。
    execution_panel.render_execution_period_section(task, task_id, scope="inspection")

    _render_aux_block(items)


def _render_aux_block(items: Sequence[Mapping[str, Any]]) -> None:
    """辅助区：全局督办统计与其他风险任务明细（默认折叠）。

    只读展示，不与当前任务混淆；原有统计分组与明细内容全部保留，
    只是从首屏移入折叠区，避免长列表挤压主业务线。
    注意：Streamlit 不允许嵌套 expander，因此这里不再嵌套「完整督办明细」。
    """
    with st.expander(SECTION_AUX, expanded=False):
        st.caption(CAPTION_AUX)
        if not items:
            st.success(HINT_NO_SUPERVISION)
            return

        st.markdown(f"**{AUX_KPI_TITLE}**")
        _render_supervision_summary(items)

        st.markdown(f"**{AUX_DETAIL_TITLE}（共 {len(items)} 项）**")
        for item in items:
            _render_supervision_item(item)


def _supervision_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """按 SUMMARY_GROUPS 统计督办条数。

    数量直接来自 execution_check.supervision_items() 的结果，
    不新增统计口径，也不做任何推测；未归入任何分组的类型不计入（当前不存在）。
    """
    counts = {label: 0 for label, _desc, _color, _kinds in SUMMARY_GROUPS}
    for item in items:
        for label, _desc, _color, kinds in SUMMARY_GROUPS:
            if item.get("kind") in kinds:
                counts[label] += 1
                break
    return counts


def _render_supervision_summary(items: Sequence[Mapping[str, Any]]) -> None:
    """紧凑风险摘要：四类关注事项的数量（真实计数）。"""
    counts = _supervision_counts(items)
    cards.kpi_row(
        [
            {
                "label": label,
                "value": counts[label],
                "desc": desc,
                "color": color,
            }
            for label, desc, color, _kinds in SUMMARY_GROUPS
        ]
    )


def _render_supervision_focus(items: Sequence[Mapping[str, Any]], task_id: int) -> None:
    """AI 持续督办结论：针对**当前任务**给出判断，再提示全局情况。

    只使用当前 task_id 在真实督办清单中的条目（同一条目由
    execution_check.supervision_items() 产出），没有条目时说明未触发规则的原因，
    不虚构风险、不编造建议来源。全局其他风险只给一行提示，明细在辅助区。
    """
    current = next(
        (item for item in items if int(item.get("task_id") or 0) == int(task_id)), None
    )

    if current is None:
        if closure_store.is_closed(int(task_id)):
            st.success(FOCUS_CLOSED)
        else:
            st.success(FOCUS_OK)
        if items:
            st.caption(FOCUS_OTHERS.format(count=len(items)))
        return

    owner = (
        f"{current.get('department') or labels.UNKNOWN_TEXT}"
        f" / {current.get('owner') or labels.UNKNOWN_TEXT}"
    )
    lines = [
        f"任务：{current.get('title') or labels.UNKNOWN_TEXT}（{owner}）",
        f"风险类型：{current.get('tag') or labels.UNKNOWN_TEXT}",
        f"判断依据：{current.get('evidence') or labels.UNKNOWN_TEXT}",
        f"建议下一步：{current.get('suggestion') or labels.UNKNOWN_TEXT}",
    ]
    text = f"**{FOCUS_TITLE}**\n\n" + "  \n".join(lines)
    if current.get("tone") == "danger":
        st.error(text)
    else:
        st.warning(text)

    others = [item for item in items if int(item.get("task_id") or 0) != int(task_id)]
    if others:
        st.caption(FOCUS_OTHERS.format(count=len(others)))


def _render_supervision_item(item: Mapping[str, Any]) -> None:
    """完整督办明细中的一条（任务 / 部门 / 负责人 / 风险类型 / 依据 / 建议）。"""
    owner = (
        f"{item.get('department') or labels.UNKNOWN_TEXT}"
        f" / {item.get('owner') or labels.UNKNOWN_TEXT}"
    )
    st.markdown(
        f"- **【{item.get('tag') or labels.UNKNOWN_TEXT}】"
        f"{item.get('title') or labels.UNKNOWN_TEXT}**　（{owner}）"
    )
    st.caption(
        f"依据：{item.get('evidence') or labels.UNKNOWN_TEXT}"
        f"　｜　建议：{item.get('suggestion') or labels.UNKNOWN_TEXT}"
    )


# ============================================================
# 渲染层
# ============================================================


def render() -> None:
    """渲染「执行反馈与检查」页面（v3.0 第四阶段：三模块 + 辅助区）。

    页面主线（同一页完成，不要求管理者跳转）：
        模块一：任务信息与执行状态（查看范围 + 任务选择 + 紧凑任务卡）
        模块二：执行反馈与 AI 持续督办（反馈摘要 → 督办结论 → 折叠的录入与处置）
        模块三：01 办理结果与办结标准 → 02 AI 核验与正式闭环
        辅助区：全局督办统计与其他风险任务（默认折叠）

    展示层级：详细表单、完整办结标准、核验历史与闭环表单按需展开；
    业务判定规则、调用顺序与数据来源完全不变。
    """
    cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)

    flash = state.pop_flash()
    if flash:
        st.success(flash)

    closed_ids = {int(x["task_id"]) for x in closure_store.list_closures()}
    scope = st.radio(
        "查看范围",
        list(SCOPE_OPTIONS),
        horizontal=True,
        key="cf_inspection_scope",
    )
    tasks = _tasks_in_scope(scope, closed_ids)
    if not tasks:
        cards.empty_state(
            _empty_title_for(scope),
            _empty_hint_for(scope),
            "前往会议材料与任务生成" if scope == SCOPE_PENDING else None,
        )
        return

    options = {tables.task_option_label(t): t for t in tasks}
    picked = st.selectbox("选择任务", list(options), key="cf_inspection_task")
    task = options[picked]
    task_id = int(task["id"])

    # ---------- 模块一：任务信息与执行状态 ----------
    st.write("")
    cards.section(SECTION_TASK_CONTEXT, CAPTION_TASK_CONTEXT)
    _render_task_header(task, task_id)

    st.divider()
    # ---------- 模块二：执行反馈与 AI 持续督办 ----------
    _render_exec_supervision_block(task, task_id)

    st.divider()
    # ---------- 模块三：核验依据（01）→ AI 核验与正式闭环（02）----------
    _render_result_block(task_id)

    st.divider()
    _render_verify_close_block(task, task_id)


def _tasks_in_scope(scope: str, closed_ids: set[int]) -> list[dict[str, Any]]:
    """按查看范围取任务。

    明确区分三件容易混淆的事：
        未下发 —— 还没进入执行阶段，**不参与执行反馈统计**；
        已下发 —— 执行期，是反馈录入与持续督办的对象；
        已完成 / 已闭环 —— 前者是执行状态，后者是独立业务事实（task_closures）。
    「待办理」保留为默认队列（未闭环），便于日常操作。
    """
    everything = sqlite_db.list_tasks()
    if scope == SCOPE_PENDING:
        return [t for t in everything if int(t["id"]) not in closed_ids]
    if scope == SCOPE_CLOSED:
        return [t for t in everything if int(t["id"]) in closed_ids]
    if scope == SCOPE_DISPATCHED:
        return [t for t in everything if submission_check.is_dispatched(int(t["id"]))]
    if scope == SCOPE_NOT_DISPATCHED:
        return [t for t in everything if not submission_check.is_dispatched(int(t["id"]))]
    if scope == SCOPE_COMPLETED:
        return [t for t in everything if str(t.get("status") or "") == "completed"]
    return everything


def _empty_title_for(scope: str) -> str:
    if scope == SCOPE_CLOSED:
        return "暂无已闭环任务"
    if scope == SCOPE_NOT_DISPATCHED:
        return "暂无未下发任务"
    return "暂无可检查的任务"


def _empty_hint_for(scope: str) -> str:
    if scope == SCOPE_CLOSED:
        return "尚未有任务完成正式闭环。满足核验通过与全部前置条件后可在此办理。"
    if scope == SCOPE_COMPLETED:
        return "暂无已完成任务。可在「执行反馈」中把已办结任务标记为已完成。"
    if scope == SCOPE_NOT_DISPATCHED:
        return "所有已入库任务都已下发。可到「任务督办中心」继续下发新任务。"
    if scope == SCOPE_ALL:
        return "请先在「会议材料与任务生成」识别事项并入库。"
    return "待办理队列为空。可切换到「已闭环」查看已办结事项。"


# ------------------------------------------------------------
# 顶部：紧凑任务卡（任务名称 / 负责人 / 截止时间 / 当前阶段）
# ------------------------------------------------------------


def _render_task_header(task: Mapping[str, Any], task_id: int) -> None:
    """顶部信息卡。

    只放五件事：任务名称、责任部门、负责人、截止时间、当前阶段。
    目的是让评审在首屏一眼看到「这是哪个任务、谁负责、什么时候到期、走到哪一步」，
    同时避免与下方功能区重复展示同样的任务字段。
    """
    title = str(task.get("title") or labels.UNKNOWN_TEXT)
    badge = styles.risk_badge_html(str(task.get("risk_level") or ""))
    rows = [
        (LBL_DEPT, str(task.get("department") or labels.UNKNOWN_TEXT)),
        (LBL_OWNER, str(task.get("owner") or labels.UNKNOWN_TEXT)),
        (LBL_DEADLINE, str(task.get("deadline") or labels.UNKNOWN_TEXT)),
        (STAGE_TITLE, STAGE_LABELS[_current_stage(task_id)]),
    ]
    body = "".join(
        f'<div class="cf-file-row"><span class="cf-file-label">{escape(label)}</span>'
        f"<span>{escape(value)}</span></div>"
        for label, value in rows
    )
    st.markdown(
        f'<div class="cf-card"><div class="cf-file-head" style="color:{styles.TEXT_MAIN}">'
        f"<span>{escape(title)}</span>{badge}</div>{body}</div>",
        unsafe_allow_html=True,
    )
    st.caption(STAGE_CAPTION)


# ------------------------------------------------------------
# 功能区 01：办理结果与办结标准
# ------------------------------------------------------------


def _render_result_block(task_id: int) -> None:
    """办结标准（含编辑）+ 负责人提交的办理结果，集中在一个功能区。"""
    cards.section(SECTION_RESULT, CAPTION_RESULT)
    _render_criteria(task_id)
    st.write("")
    _render_submission_result(task_id)


def _render_criteria(task_id: int) -> None:
    """办结标准及确认状态 + 就地编辑（保留原有保存与确认机制）。

    先展示「当前标准摘要与确认状态」，条目明细与编辑入口放进折叠区：
    这样未确认标准不会被误读为已生效的正式办结依据，也不会在演示首屏
    把「核验 / 闭环」这类关键操作挤出可视区域。
    """
    meta = task_metadata_store.get_metadata(task_id)
    current = list(meta.get("closure_criteria") or [])
    confirmed_text = "已确认" if meta.get("closure_confirmed") else labels.CLOSURE_UNCONFIRMED_HINT

    st.markdown(f"**{SUB_CRITERIA}**")
    if current:
        st.caption(
            f"共 {len(current)} 条　｜　"
            f"标准来源：{labels.closure_source_label(meta.get('closure_source'))}　｜　"
            f"确认状态：{confirmed_text}"
        )
    else:
        st.caption("尚未设置办结标准。缺少已确认的办结标准时，正式闭环会被拒绝。")

    with st.expander(EXPANDER_CRITERIA, expanded=False):
        if current:
            for item in current:
                st.markdown(f"- {item}")
        else:
            st.caption("可在下方按行填写办结标准并保存确认。")

        edit_col, source_col, save_col = st.columns([3, 2, 1])
        with edit_col:
            items = st.text_area(
                LBL_CRITERIA,
                value="\n".join(str(x) for x in current),
                height=76,
                key="cf_criteria_items",
                help="每行一条办结标准。",
            )
        with source_col:
            source_label = st.selectbox(
                LBL_SOURCE, list(CRITERIA_SOURCE_OPTIONS), key="cf_criteria_source"
            )
            st.caption("保存后即确认为强制闭环依据。")
        with save_col:
            st.write("")
            saved = st.button(
                BTN_SAVE_CRITERIA, key="cf_criteria_save", use_container_width=True
            )

    if not saved:
        return

    parsed = [x.strip() for x in items.splitlines() if x.strip()]
    task_metadata_store.set_closure_criteria(
        task_id,
        parsed,
        closure_source=CRITERIA_SOURCE_OPTIONS[source_label],
        closure_confirmed=True,
    )
    state.set_flash("办结标准已保存并确认为强制依据。")
    st.rerun()


def _render_submission_result(task_id: int) -> None:
    """负责人提交的办理结果 + 当前回执状态与材料。

    compact=True：任务名称 / 负责人 / 截止时间已由顶部任务卡承载，
    这里不再重复展示，只保留回执状态、办理内容、附件与提交入口。
    """
    st.markdown(f"**{SUB_RESULT}**")
    submission_view.render_check_window(task_id, compact=True)


# ------------------------------------------------------------
# 功能区 02：核验与正式闭环
# ------------------------------------------------------------


def _render_verify_close_block(task: Mapping[str, Any], task_id: int) -> None:
    """核验结论 → 核验操作 → 核验历史 → 正式闭环，集中在一个功能区。

    顺序刻意固定：先看「当前有效提交的结论」，再决定是否重新核验，
    最后才谈闭环；历史永远排在结论之后，避免旧结论被当成当前状态。
    """
    cards.section(SECTION_VERIFY_CLOSE, CAPTION_VERIFY_CLOSE)

    submission = submission_store.get_submission(task_id)

    st.markdown(f"**{SUB_CONCLUSION}**")
    current = _current_verification(task_id)
    if current is not None:
        st.caption(
            f"核验时间：{current.get('verified_at') or labels.UNKNOWN_TEXT}　｜　"
            f"核验人：{current.get('verified_by') or labels.UNKNOWN_TEXT}"
        )
        _render_verification_record(current)
    elif submission is None:
        cards.hint(["尚无回执提交记录，不存在可核验对象。请先在上方提交办理结果。"])
    else:
        cards.hint(
            [
                "当前提交版本尚未核验。核验历史中的旧版本结论不适用于当前版本，"
                "请在下方发起核验。"
            ]
        )

    if submission is not None:
        st.write("")
        _render_verify_actions(task_id)

    st.write("")
    _render_closure(task, task_id)

    # 核验历史放在最后且默认折叠：先看当前结论与闭环状态，历史仅作追溯。
    if submission is not None:
        st.write("")
        _render_history(task_id)


def _render_verify_actions(task_id: int) -> None:
    """核验操作区：核验人 / 人工裁决 / 核验说明 + 发起核验。

    核验结果不落在 session_state 里展示：完成后只记一条 flash 并 rerun，
    页面重新按数据库读「当前提交版本」的结论，从根上避免
    「已重新提交但界面仍显示旧结论」的错位。
    """
    st.markdown(f"**{SUB_VERIFY_ACTION}**")
    operator = st.text_input(LBL_OPERATOR, value=models.DEFAULT_OPERATOR, key="cf_verify_op")
    decision_label = st.selectbox(LBL_DECISION, list(DECISION_OPTIONS), key="cf_verify_decision")
    reason = st.text_area(LBL_REASON, height=68, key="cf_verify_reason")

    if not st.button(BTN_VERIFY, key="cf_verify_run", type="primary"):
        return

    outcome = run_verification(
        task_id,
        operator=operator.strip() or models.DEFAULT_OPERATOR,
        decision=DECISION_OPTIONS[decision_label],
        reason=reason.strip(),
    )
    if not outcome["ok"]:
        st.error(outcome["message"])
        return

    result = outcome["result"]
    if not result.get("ok"):
        st.warning(result.get("reason") or "当前不满足核验条件。")
        return

    state.set_flash(f"核验完成，结论为{labels.verification_label(result.get('status'))}。")
    st.rerun()


def _render_verification_record(record: Mapping[str, Any]) -> None:
    """展示一条核验记录的四维度结论、说明与处理指引。

    record 直接来自 verifications 表（当前提交版本的最新一条），
    因此页面上看到的结论与闭环闸门使用的是同一条事实。
    """
    status = str(record.get("status") or "")
    st.markdown(f"**核验结论：{labels.verification_label(status)}**")

    rows = [
        {
            "维度": labels.verification_dimension_label(key),
            "结论": "符合" if record.get(key) else "不符合",
        }
        for key in DIMENSION_KEYS
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)

    reason = str(record.get("reason") or "").strip()
    if reason:
        st.caption(f"核验说明：{reason}")

    st.write(_guidance_for(status))
    if status == models.VERIFICATION_MANUAL_REVIEW:
        st.caption("当前为人工裁决结果，仅作记录，不自动闭环。")


def _guidance_for(status: str) -> str:
    """按核验结论给出处理指引（只描述系统真实具备的路径）。"""
    if status == models.VERIFICATION_NEED_SUPPLEMENT:
        return GUIDANCE_NEED_SUPPLEMENT
    if status == models.VERIFICATION_REJECTED:
        return GUIDANCE_REJECT_REWORK
    if status == models.VERIFICATION_MANUAL_REVIEW:
        return GUIDANCE_MANUAL_REVIEW
    if status == models.VERIFICATION_APPROVED:
        return "核验已通过。满足下方闭环全部前置条件后，可办理正式闭环。"
    return GUIDANCE_PENDING


def _render_history(task_id: int) -> None:
    """核验历史（默认折叠），并显式区分「当前提交版本」与「历史版本」。

    标注为什么必要：核验结论只对当前提交版本有效。若历史里存在一条通过、
    而当前版本未通过，不标注就会让评审误以为「系统自相矛盾」。
    这里给每条非当前版本的记录标出「历史版本 + 指纹短码」，
    让矛盾观感在界面上被消解为可追溯的事实。
    """
    rows = verification_store.list_verifications(task_id)
    if not rows:
        st.caption("该任务暂无核验记录。")
        return

    current = _current_verification(task_id)
    current_id = current.get("id") if current else None
    history_count = len(rows) - (1 if current_id is not None else 0)
    if current_id is not None:
        expander_title = f"{SUB_HISTORY}（共 {len(rows)} 条：当前版本 1 条 / 历史版本 {history_count} 条）"
    else:
        expander_title = (
            f"{SUB_HISTORY}（共 {len(rows)} 条，均为历史版本，不适用于当前提交）"
        )

    with st.expander(expander_title, expanded=False):
        for row in reversed(rows):  # 最新在前，避免旧结论被误读为当前状态
            if row.get("id") == current_id:
                mark = "　**← 当前提交版本**"
            else:
                digest = str(row.get("submission_digest") or "")[:8]
                mark = f"　（历史版本 {digest or '无指纹'}）"
            st.markdown(
                f"- {row.get('verified_at')}　{labels.verification_label(row.get('status'))}"
                f"　（核验人：{row.get('verified_by') or labels.UNKNOWN_TEXT}）{mark}"
            )
            if row.get("reason"):
                st.caption(str(row["reason"]))


def _render_closure(task: Mapping[str, Any], task_id: int) -> None:
    """正式闭环：状态展示 → 前置条件 → 操作。

    闭环按钮的可用性由 _closure_blockers() 只读预告决定，与后端 close_task
    使用同一套规则常量；即便预告通过，点击后仍由 close_task 做最终闸门，
    失败时把后端返回的真实原因原样展示。
    """
    st.markdown(f"**{SUB_CLOSURE}**")

    status = closure_svc.get_closure_status(task_id)

    # ---- 已闭环 ----
    if status.get("closed"):
        closure = status.get("closure") or {}
        st.success(f"该任务已于 {closure.get('closed_at')} 正式闭环。")
        st.markdown(
            f"- 闭环人：{closure.get('closed_by') or labels.UNKNOWN_TEXT}\n"
            f"- 闭环说明：{closure.get('closure_note') or labels.UNKNOWN_TEXT}"
        )
        if status.get("stale"):
            st.warning(
                "闭环依据已过期：该任务在闭环后又有新的提交。历史闭环记录保留，"
                "但不代表当前提交版本已办结。"
            )
        st.caption(CLOSED_HINT)
        return

    # ---- 前置条件（只读预告，规则取自 closure_service）----
    blockers = _closure_blockers(task, task_id)
    st.markdown(f"**{SUB_BLOCKERS}**")
    if blockers:
        for item in blockers:
            st.markdown(f"- 不满足：{item}")
    else:
        st.markdown(f"- {labels.closure_state_label('ready')}")

    high_risk = str(task.get("risk_level") or "") == RISK_HIGH_VALUE

    can_close = not blockers
    if not can_close:
        st.caption(CLOSE_BUTTON_DISABLED_HINT)

    # 详细闭环表单按需展开：条件齐备时默认展开（一步完成闭环），
    # 条件未满足时折叠（首屏只留真实的阻断原因，按钮不可用）。
    with st.expander(EXPANDER_CLOSE_FORM, expanded=can_close):
        note = st.text_area(LBL_NOTE, height=68, key="cf_close_note")
        ack = st.text_input(LBL_ACK, key="cf_close_ack") if high_risk else ""
        operator = st.text_input(LBL_CLOSER, value=models.DEFAULT_OPERATOR, key="cf_close_op")
        submitted = st.button(
            BTN_CLOSE,
            key="cf_close_run",
            type="primary",
            disabled=not can_close,
        )

    if not submitted:
        return

    outcome = run_closure(
        task_id,
        closed_by=operator.strip() or models.DEFAULT_OPERATOR,
        closure_note=note.strip(),
        high_risk_ack=ack.strip(),
    )
    if not outcome["ok"]:
        st.error(outcome["message"])
        return

    result = outcome["result"]
    if not result.get("ok"):
        st.error(result.get("reason") or "当前不满足闭环条件，系统已阻止闭环。")
        return
    if result.get("idempotent"):
        st.info("该任务此前已完成闭环，本次未新增记录。")
        return

    state.set_flash("任务已正式闭环。")
    st.rerun()
