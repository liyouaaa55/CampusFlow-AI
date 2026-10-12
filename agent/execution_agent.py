"""ExecutionAgent：执行处置智能体（v2.0，调用大模型）。

定位：处理**已经存在于系统中的任务**，不是材料识别。
TaskAgent 解决「材料 → 任务」，ExecutionAgent 解决「任务卡住了 → 该怎么办」。

职责严格限定为四步，**不含执行**：

    Observe  读任务 + 读历史 + 判定提醒等级（复用 ReminderAgent）
    Analyze  调大模型做「情况说明 + 判断依据」
    Plan     动作由确定性规则选出（tools.execution_tool）
    Validate 交给 Action Validator 逐层校验

明确不做的事：
- 不执行任何动作（ESCALATE 只产出计划，真正提级由后续人工确认流程调用
  tools.execution_tool.execute() 完成）；
- 不修改 owner / department / deadline / title / status / risk_level；
- 不发送任何外部消息；
- 不直接写 SQL、不自行建立数据库连接——所有数据库写入一律经
  database.sqlite_db 的公开接口。

**为什么用模块级函数而不是 class：**
本项目现有四个 Agent（task_agent / planner_agent / reminder_agent /
report_agent）全部是「模块 + 无状态函数 + 通过参数注入依赖」的风格，
入口形如 task_agent.extract_tasks(text, source_type, llm_fn)。
为保持一致、避免为接口形式强行重构，本模块沿用同一风格：
analyze(task_id, llm_fn=..., db_path=..., today=...)。

**大模型无权决定任何动作。** 这是本模块最重要的设计约束，由三层共同保证：
1. Prompt 层：EXECUTION_ANALYSIS_SYSTEM 明确禁止模型输出动作与受保护字段；
2. 归一化层：tools.execution_tool.normalize_plan() 丢弃模型返回的
   action / params，一律用 select_action() 的规则结果覆盖；
3. 校验层：validate_plan() 的 L1/L2/L3/L5 逐层拦截。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent import reminder_agent
from config import settings
from database import (
    closure_store,
    models,
    sqlite_db,
    submission_store,
    task_metadata_store,
    verification_store,
)
from llm import client, prompts
from tools import execution_tool

# 大模型调用函数签名：(system_prompt, user_prompt) -> JSON 对象
# 与 task_agent.LlmFn 保持一致，便于测试注入。
LlmFn = Callable[[str, str], Any]

# 送入分析上下文的最近历史事件条数上限。
# 历史再多也不无限膨胀 Prompt：处置判断只关心近期发生了什么，
# 更久远的事件对本轮决策几乎没有增量价值。
HISTORY_LIMIT: int = 20

# 历史为空时的占位说明（避免 Prompt 里出现空的「历史事件」段落）。
NO_HISTORY_TEXT: str = "（该任务暂无历史操作事件）"

# 办结标准 / 办理进度为空时的占位说明（与历史占位同口径）。
NO_CRITERIA_TEXT: str = "（该任务尚未设置办结标准与办理要求）"
NO_SUBMISSION_TEXT: str = "（该任务尚未提交工作回执）"

# 送入分析上下文的任务字段白名单。
# 只暴露与「判断当前执行风险」直接相关的字段，不整体倾倒 task 记录。
TASK_CONTEXT_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "department",
    "owner",
    "deadline",
    "priority",
    "status",
    "risk_level",
    "description",
)

# 送入分析上下文的日志字段白名单。
HISTORY_CONTEXT_FIELDS: tuple[str, ...] = ("action", "operator", "remark", "created_at")


class ExecutionAnalysisError(RuntimeError):
    """处置分析失败。

    覆盖两类情况：
    1. 大模型调用失败（超时、返回非法 JSON、鉴权失败等）——沿用 llm.client 的
       异常体系，本类不吞异常、不伪造结果；
    2. 大模型返回的结构不满足约定（缺 situation、reasons 为空、元素非字符串等）。

    设计取舍：**宁可明确失败，也不伪造 situation / reasons。**
    凭空生成的解释会让上层与评委误以为分析成功，是更严重的错误。
    上层 UI 应捕获本异常并提示「智能分析暂时失败，请稍后重试」。
    """


# ============================================================
# 一、对外接口
# ============================================================


def analyze(
    task_id: int,
    llm_fn: LlmFn | None = None,
    db_path: str | Path | None = None,
    today: date | None = None,
) -> dict[str, Any] | None:
    """对单个任务做执行处置分析。

    参数：
        task_id  待分析的任务主键；
        llm_fn   大模型调用函数，默认使用 llm.client.chat_json；
                 显式注入便于测试（测试一律注入假模型，不调真实 API）；
        db_path  数据库路径，默认 settings.DB_PATH；
        today    基准日期，默认今天；显式传入便于测试与复算。

    返回：
        None            —— 无需处置（任务已正式闭环 / 已完成 / 缺少截止时间 /
                           提醒等级正常）。这些情况**不会调用大模型**。
        dict            —— 处置结果，固定包含以下键：
                           ok         是否得到可执行的处置计划
                           task_id    任务主键
                           action     规则选出的动作（被拒绝时也会给出，便于 UI 解释）
                           params     动作参数（由规则生成）
                           need_confirm  恒为 True
                           level      提醒等级
                           days_left  剩余天数
                           situation  大模型给出的情况说明
                           reasons    大模型给出的判断依据
                           risk_note  规则生成的风险说明
                           plan       规范化计划（ok=True 时可交给人工确认）
                           error      拒绝原因（ok=False 时有值）

    异常：
        sqlite_db.TaskNotFoundError  任务不存在；
        ExecutionAnalysisError      大模型调用失败或返回结构不合法。
    """
    base_date = today or date.today()

    # ---------- STEP 1 读取任务 ----------
    task = sqlite_db.get_task(task_id, db_path=db_path)
    if task is None:
        # 沿用项目既有异常体系，不吞异常、不返回空结果
        raise sqlite_db.TaskNotFoundError(f"任务不存在：id={task_id}")

    # ---------- STEP 1.5 正式闭环早退（不调用大模型、不写日志） ----------
    # 已正式闭环的任务不再接受督办分析。判定依据是 task_closures 表是否存在记录
    # （与 tools/task_dispatch.dispatch_task 完全同一口径）。
    # **绝不**用 tasks.status == "completed" 代替：闭环不回写 status，二者互不蕴含。
    if closure_store.is_closed(int(task_id), db_path=db_path):
        return None

    # ---------- STEP 2 早退判断（不调用大模型） ----------
    # 复用 ReminderAgent：已完成、缺截止时间、等级正常三种情况都在这里返回 None。
    reminder = reminder_agent.check_task(task_id, today=base_date, db_path=db_path)
    if reminder is None:
        return None
    level = str(reminder.get("level") or "")
    if level not in execution_tool.DRAFT_NOTIFY_LEVELS:
        # normal（以及任何未知等级）都不需要处置
        return None

    days_left = reminder.get("days_left")
    days_text = "NA" if days_left is None else str(days_left)

    # ---------- STEP 3 读取历史 ----------
    logs = sqlite_db.get_logs(task_id, db_path=db_path)

    # ---------- STEP 3.5 读取办结标准与办理进度（只读，供模型参考） ----------
    # 这两项是 v2.2 已落地的既定业务事实：分析若不感知，
    # 会在「已提交回执 / 核验未通过 / 已设办结标准」时给出错误结论。
    criteria_block = _render_criteria_context(task_id, db_path)
    progress_block = _render_progress_context(task_id, db_path)
    action = execution_tool.select_action(level, task, logs)
    if action is None:
        return None

    # ---------- STEP 5 调用大模型做情况理解 ----------
    system_prompt = prompts.EXECUTION_ANALYSIS_SYSTEM
    user_prompt = prompts.EXECUTION_ANALYSIS_USER.format(
        today=base_date.isoformat(),
        level=level,
        days_left=days_text,
        task_block=_render_task_context(task),
        criteria_block=criteria_block,
        progress_block=progress_block,
        history_block=_render_history(logs),
    )
    caller = llm_fn or client.chat_json
    try:
        raw = caller(system_prompt, user_prompt)
    except client.LLMError as exc:
        # 统一包装为 ExecutionAnalysisError：上层 UI 只需捕获一种异常即可，
        # 原始异常通过 __cause__ 保留，便于排查。
        # 关键点：此处绝不伪造 situation / reasons，直接失败。
        raise ExecutionAnalysisError(f"大模型调用失败：{exc}") from exc

    # ---------- STEP 6 校验大模型输出 ----------
    situation, reasons = _parse_analysis(raw)

    # ---------- STEP 7 构造计划（模型给的 action/params 在此被丢弃） ----------
    plan = execution_tool.normalize_plan(
        task,
        level,
        raw={"situation": situation, "reasons": reasons},
        risk_note=build_risk_note(action, level, days_text),
    )
    plan["days_left"] = days_left

    # ---------- STEP 8 交给 Validator ----------
    validation = execution_tool.validate_plan(
        task, plan, logs, today=base_date
    )

    # ---------- STEP 9 记录 analyze 事件（必须在人工确认之前） ----------
    # 三道前置条件全部满足才写：分析成功 + 计划构建成功 + 校验通过。
    # 任一环节失败都不留痕，避免事件链里出现「分析了但没结论」的幽灵记录。
    if validation.ok:
        sqlite_db.append_log(
            task_id,
            action=models.ACTION_ANALYZE,
            operator=models.DEFAULT_OPERATOR,
            remark=execution_tool.build_remark(
                action, level, days_left, situation
            ),
            db_path=db_path,
        )

    # ---------- STEP 10 返回结果（不执行任何动作） ----------
    return {
        "ok": validation.ok,
        "task_id": task_id,
        "action": action,
        "params": dict(plan.get("params") or {}),
        "need_confirm": True,
        "level": level,
        "days_left": days_left,
        "situation": situation,
        "reasons": list(reasons),
        "risk_note": str(plan.get("risk_note") or ""),
        "plan": plan if validation.ok else None,
        "error": None if validation.ok else validation.reason,
    }


# ============================================================
# 二、风险说明（规则生成，大模型无权干预）
# ============================================================


def build_risk_note(action: str, level: str, days_text: str) -> str:
    """按动作与提醒等级生成风险说明。

    全部为固定文案，绝不由大模型生成——风险说明是对「这次改动意味着什么」
    的承诺，必须与实际执行行为严格一致。
    """
    if action == execution_tool.ACTION_ESCALATE:
        return (
            "任务已逾期且当前风险等级尚未达到 red，"
            "确认后将把风险等级提升为 red（不会修改任务状态）。"
        )
    if action == execution_tool.ACTION_DRAFT_NOTIFY:
        return (
            f"任务当前处于 {reminder_agent.level_label(level)} 阶段（剩余 {days_text} 天），"
            "建议生成督办提醒草稿；系统不会自动发送任何外部消息。"
        )
    if action == execution_tool.ACTION_REQUEST_INFO:
        return "任务缺少关键执行信息（负责人或截止时间），需要人工补充；系统不会自动修改任务字段。"
    return "当前无需处置。"


# ============================================================
# 三、内部实现
# ============================================================


def _parse_analysis(raw: Any) -> tuple[str, list[str]]:
    """校验并提取大模型的分析结果。

    校验项（任一不满足即抛 ExecutionAnalysisError）：
    1. 返回值必须是映射结构；
    2. situation 必须是非空字符串；
    3. reasons 必须是列表；
    4. 列表元素必须全部是字符串——出现非字符串即判定为协议违规；
    5. 去除空串与纯空白；
    6. 去重后至少保留 1 条有效依据。

    关于第 4 条的严格性：这里刻意不做「过滤掉坏元素」的宽容处理。
    大模型返回结构不符合约定本身就是需要暴露的问题，静默清洗会让
    协议漂移变得不可见。
    """
    if not isinstance(raw, Mapping):
        raise ExecutionAnalysisError(
            f"模型返回结构非法：期望对象，实际为 {type(raw).__name__}。"
        )

    situation = raw.get("situation")
    if not isinstance(situation, str) or not situation.strip():
        raise ExecutionAnalysisError("模型返回缺少有效的 situation。")

    raw_reasons = raw.get("reasons")
    if not isinstance(raw_reasons, list):
        raise ExecutionAnalysisError(
            f"模型返回的 reasons 非法：期望列表，实际为 {type(raw_reasons).__name__}。"
        )

    reasons: list[str] = []
    for index, item in enumerate(raw_reasons):
        if not isinstance(item, str):
            raise ExecutionAnalysisError(
                f"模型返回的 reasons[{index}] 非字符串：{type(item).__name__}。"
            )
        text = item.strip()
        if text and text not in reasons:
            reasons.append(text)

    if not reasons:
        raise ExecutionAnalysisError("模型返回的 reasons 为空，无法形成判断依据。")

    return situation.strip(), reasons


def _render_task_context(task: Mapping[str, Any]) -> str:
    """把任务字段渲染成 JSON 块（只取白名单字段，缺失值显式标注）。"""
    context: dict[str, Any] = {}
    for field in TASK_CONTEXT_FIELDS:
        value = task.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            context[field] = None
        else:
            context[field] = value
    return json.dumps(context, ensure_ascii=False, indent=2)


def _render_history(logs: Sequence[Mapping[str, Any]]) -> str:
    """把历史事件渲染成 JSON 数组块。

    只取白名单字段，且**在函数内部**强制截断到最近 HISTORY_LIMIT 条——
    上限保证必须内建在渲染逻辑里，否则任何新调用方忘记截断就会让 Prompt 膨胀。
    """
    if not logs:
        return NO_HISTORY_TEXT

    recent = [entry for entry in list(logs)[-HISTORY_LIMIT:] if isinstance(entry, Mapping)]
    if not recent:
        return NO_HISTORY_TEXT

    rendered = [
        {field: entry.get(field) for field in HISTORY_CONTEXT_FIELDS} for entry in recent
    ]
    return json.dumps(rendered, ensure_ascii=False, indent=2)


def _render_criteria_context(task_id: int, db_path: str | Path | None) -> str:
    """渲染办结标准与办理要求（只读）。

    数据来自 task_metadata_store（v2.2 人工确认的办结标准）；
    未设置时返回显式占位，不臆造标准。
    """
    meta = task_metadata_store.get_metadata(int(task_id), db_path=db_path)
    payload = {
        "execution_requirements": list(meta.get("execution_requirements") or []),
        "closure_criteria": list(meta.get("closure_criteria") or []),
        "closure_confirmed": bool(meta.get("closure_confirmed")),
    }
    if not payload["execution_requirements"] and not payload["closure_criteria"]:
        return NO_CRITERIA_TEXT
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _render_progress_context(task_id: int, db_path: str | Path | None) -> str:
    """渲染办理进度：工作回执摘要 + 最近一次核验结果（只读）。

    只暴露结论性字段（是否已提交、核验状态与原因），
    不倾倒回执全文；缺失时显式说明，不臆造「已提交 / 已核验」。
    """
    submission = submission_store.get_submission(int(task_id), db_path=db_path)
    if submission is None:
        return NO_SUBMISSION_TEXT

    latest = verification_store.latest_verification(int(task_id), db_path=db_path)
    payload = {
        "submitted": True,
        "submitted_at": submission.get("submitted_at"),
        "submission_excerpt": (str(submission.get("content") or "")[:200]),
        "latest_verification_status": (latest or {}).get("status"),
        "latest_verification_reason": (latest or {}).get("reason"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ============================================================
# 四、v3.0 第一阶段：下发前检查分析（与提醒等级无关）
# ============================================================
#
# 为什么需要独立入口：
#   analyze() 是「执行期处置分析」，受提醒等级驱动——未临期/未逾期直接返回 None。
#   而管理者在下发之前需要的是「任务清不清楚、能不能下发、还缺什么」，
#   这与是否临期无关。把两者混用，正常进度的新任务必然被判成「无需处置」，
#   这正是旧版「智能分析经常显示没有必要分析」的根因。

# 关键执行信息的中文名（规则层与 Prompt 共用）
MISSING_FIELD_LABELS: dict[str, str] = {
    "title": "任务标题",
    "department": "责任部门",
    "owner": "负责人",
    "deadline": "截止时间",
    "description": "执行要求",
}

# 下发前结论
READINESS_READY: str = "ready"
READINESS_NEEDS_SUPPLEMENT: str = "needs_supplement"
READINESS_RISKY: str = "risky"
READINESS_CLOSED: str = "closed"
READINESS_LEVELS: tuple[str, ...] = (
    READINESS_READY,
    READINESS_NEEDS_SUPPLEMENT,
    READINESS_RISKY,
)

# 问题严重度与归属字段（封闭集合，模型给非法值一律回落）
ISSUE_SEVERITIES: tuple[str, ...] = ("high", "medium", "low")
ISSUE_FIELDS: tuple[str, ...] = (*MISSING_FIELD_LABELS, "none")

# 缺失这些字段时视为「暂不具备下发条件」（与 task_dispatch 的硬门槛口径一致：
# 负责人是硬门槛；标题与截止时间缺失同样必须先补充）
BLOCKING_FIELDS: tuple[str, ...] = ("title", "owner", "deadline")


def detect_missing_fields(task: Mapping[str, Any]) -> list[str]:
    """规则层：列出关键执行信息中缺失的字段。

    确定性判断，不依赖模型；顺序固定，便于界面稳定展示。
    """
    missing: list[str] = []
    for field in ("title", "department", "owner", "deadline", "description"):
        value = task.get(field)
        if value is None or not str(value).strip():
            missing.append(field)
    return missing


def analyze_before_dispatch(
    task_id: int,
    llm_fn: LlmFn | None = None,
    db_path: str | Path | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """下发前检查分析：判断任务是否清楚、能否下发、还缺什么。

    与 analyze() 的差别（设计要点）：
    - **与提醒等级无关**：不会因为「未临期 / 未逾期」而跳过分析；
    - **必定调用大模型**（任务不存在或已闭环除外）；
    - **失败与「无需处理」严格区分**：模型失败或结构非法一律抛
      ExecutionAnalysisError，由调用方展示为「分析未成功」，
      绝不返回 None 被界面误读成「没有必要分析」。

    返回（成功）：
        ok / kind("analysis") / executed(True) / task_id / readiness /
        summary / issues / missing_fields / blocking / model_used /
        level / days_left / error("")

    已闭环：返回 kind="closed"、executed=False（不调用模型）。
    异常：sqlite_db.TaskNotFoundError 任务不存在；
         ExecutionAnalysisError 模型调用失败或返回结构不合法。
    """
    base_date = today or date.today()
    task = sqlite_db.get_task(task_id, db_path=db_path)
    if task is None:
        raise sqlite_db.TaskNotFoundError(f"任务不存在：id={task_id}")

    if closure_store.is_closed(int(task_id), db_path=db_path):
        return {
            "ok": False,
            "kind": "closed",
            "executed": False,
            "task_id": int(task_id),
            "readiness": READINESS_CLOSED,
            "summary": "该任务已正式闭环，无需再做下发前分析。",
            "issues": [],
            "missing_fields": [],
            "blocking": False,
            "model_used": False,
            "level": "",
            "days_left": None,
            "error": "",
        }

    logs = sqlite_db.get_logs(task_id, db_path=db_path)
    missing_fields = detect_missing_fields(task)
    level, days_left = _predispatch_level(task, base_date)

    user_prompt = prompts.PREDISPATCH_ANALYSIS_USER.format(
        today=base_date.isoformat(),
        task_block=_render_task_context(task),
        criteria_block=_render_criteria_context(task_id, db_path),
        missing_block=_render_missing_block(missing_fields),
        history_block=_render_history(logs),
    )
    caller = llm_fn or client.chat_json
    try:
        raw = caller(prompts.PREDISPATCH_ANALYSIS_SYSTEM, user_prompt)
    except client.LLMError as exc:
        raise ExecutionAnalysisError(f"大模型调用失败：{exc}") from exc

    readiness, summary, issues = _parse_predispatch(raw)
    issues = _merge_missing_issues(issues, missing_fields)

    return {
        "ok": True,
        "kind": "analysis",
        "executed": True,
        "task_id": int(task_id),
        "readiness": readiness,
        "summary": summary,
        "issues": issues,
        "missing_fields": missing_fields,
        # 规则层缺失项一律构成「暂不可直接下发」，不受模型结论影响
        "blocking": bool(missing_fields) or readiness != READINESS_READY,
        "model_used": True,
        "level": level,
        "days_left": days_left,
        "error": "",
    }


def _predispatch_level(task: Mapping[str, Any], base_date: date) -> tuple[str, int | None]:
    """计算提醒等级（仅作为参考信息展示，不参与是否分析的门槛判断）。"""
    days_left = reminder_agent.compute_days_left(task.get("deadline"), base_date)
    if days_left is None:
        return "", None
    return reminder_agent.compute_level(days_left), days_left


def _render_missing_block(missing_fields: Sequence[str]) -> str:
    """把规则层缺失项渲染成 Prompt 文本块。"""
    if not missing_fields:
        return "（未发现关键信息缺失）"
    return "\n".join(
        f"- {MISSING_FIELD_LABELS.get(field, field)}" for field in missing_fields
    )


def _parse_predispatch(raw: Any) -> tuple[str, str, list[dict[str, Any]]]:
    """校验并提取模型的下发前分析结果。

    校验不通过一律抛 ExecutionAnalysisError——不下发、不伪造结论。
    """
    if not isinstance(raw, Mapping):
        raise ExecutionAnalysisError(
            f"模型返回结构非法：期望对象，实际为 {type(raw).__name__}。"
        )

    readiness = str(raw.get("readiness") or "").strip()
    if readiness not in READINESS_LEVELS:
        raise ExecutionAnalysisError(f"模型返回的 readiness 非法：{readiness!r}。")

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ExecutionAnalysisError("模型返回缺少有效的 summary。")

    raw_issues = raw.get("issues")
    if not isinstance(raw_issues, list):
        raise ExecutionAnalysisError(
            f"模型返回的 issues 非法：期望列表，实际为 {type(raw_issues).__name__}。"
        )

    issues: list[dict[str, Any]] = []
    for index, item in enumerate(raw_issues):
        if not isinstance(item, Mapping):
            raise ExecutionAnalysisError(
                f"issues[{index}] 非对象：{type(item).__name__}。"
            )
        problem = str(item.get("problem") or "").strip()
        if not problem:
            raise ExecutionAnalysisError(f"issues[{index}] 缺少 problem。")
        severity = str(item.get("severity") or "medium").strip().lower()
        field = str(item.get("field") or "none").strip().lower()
        issues.append(
            {
                "problem": problem,
                "cause": str(item.get("cause") or "").strip(),
                "evidence": str(item.get("evidence") or "").strip(),
                "suggestion": str(item.get("suggestion") or "").strip(),
                "severity": severity if severity in ISSUE_SEVERITIES else "medium",
                "field": field if field in ISSUE_FIELDS else "none",
            }
        )

    return readiness, summary.strip(), issues


def _merge_missing_issues(
    issues: list[dict[str, Any]], missing_fields: Sequence[str]
) -> list[dict[str, Any]]:
    """把规则层确认缺失、但模型未提及的字段补成一条 issue。

    设计取舍：模型漏报不影响「缺什么」这一客观事实，因此由代码兜底补齐，
    保证管理者一定能看到全部缺失项，而不是依赖模型是否恰好提到。
    """
    covered = {str(item.get("field")) for item in issues}
    merged = list(issues)
    for field in missing_fields:
        if field in covered:
            continue
        label = MISSING_FIELD_LABELS.get(field, field)
        merged.append(
            {
                "problem": f"缺少{label}",
                "cause": f"任务信息中「{label}」为空",
                "evidence": f"任务信息 {field} 为 null",
                "suggestion": f"请补充{label}后再下发",
                "severity": "high" if field in BLOCKING_FIELDS else "medium",
                "field": field,
            }
        )
    return merged
