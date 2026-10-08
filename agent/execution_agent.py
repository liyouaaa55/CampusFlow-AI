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
from database import models, sqlite_db
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
        None            —— 无需处置（任务已完成 / 缺少截止时间 / 提醒等级正常）。
                           这三种情况**不会调用大模型**。
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

    # ---------- STEP 4 由规则确定动作（与 LLM 无关） ----------
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
