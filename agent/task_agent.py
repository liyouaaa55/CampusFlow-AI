"""TaskAgent：事项识别智能体（本项目的核心 Agent）。

职责：从非结构化文本（会议纪要、公文、Excel 台账、TXT、手动输入）中识别
**可执行事项**，输出结构统一的任务列表。它不是总结工具。

输入：统一为 `str`。
输出：`list[dict]`，每个字典字段固定为
    title / department / owner / deadline / priority / status / description

示例：
    [{"title": "举办智能机器人创新周", "department": "学工办", "owner": "张老师",
      "deadline": "2026-09-30", "priority": "high", "status": "pending",
      "description": "负责制定活动实施方案"}]

关键约定：
- **结构化台账走确定性路径**：若材料具备明确表头结构（事项/部门/负责人/时间/状态等列），
  则由 tools/ledger_parser 逐行直接映射，**不调用大模型**——表格已把行边界与字段值写死，
  再让模型判断一次「这行算不算任务、算完成还是待办」不可复现（实测同一表格连续 5 次
  调用会丢行、状态与负责人会跳变）。识别不出表头时自动退回大模型路径，行为不变。
- 缺失字段一律返回 None，不做任何猜测（与 Prompt 中的约束一致）；
- status 按材料中的**明确表达**归一化：completed / processing / pending；
  材料明确表示「已取消 / 已作废」的事项不构成可执行事项，直接丢弃；
  未明确表达时落回 settings.DEFAULT_STATUS，绝不猜测成已完成或进行中；
- **只抽取仍需后续执行的新事项**：工作进展、完成情况、阶段性汇报类材料
  默认不产出任务，除非其中明确出现后续执行要求；
- priority 仅接受 high / medium / low，非法值一律置为 None，
  由 PlannerAgent 依据 knowledge/rules 中的关键词规则兜底（第六阶段实现）；
- 识别前会读取 knowledge/organization/departments.json 注入 System Prompt，
  用于把部门口径对齐到本校规范名称（知识库缺失时自动回退基础 Prompt）。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from config import settings
from knowledge import KnowledgeError, organization_block
from llm import client, prompts
from tools import directional_guard, ledger_parser, parser_tool, rescue

# 输出字段顺序（与设计文档保持一致，便于前端直接展示）
TASK_FIELDS: tuple[str, ...] = (
    "title",
    "department",
    "owner",
    "deadline",
    "priority",
    "status",
    "description",
)

# 来源类型中文名，仅用于 Prompt 提示
SOURCE_TYPE_LABELS: dict[str, str] = {
    "meeting": "会议纪要",
    "document": "公文 / 规章制度",
    "excel": "表格台账",
    "text": "纯文本",
    "manual": "手动输入",
}

# 模型可能用这些写法表示「没有信息」，统一视为缺失（不得猜测）
_NULL_TOKENS: frozenset[str] = frozenset(
    {"", "null", "none", "n/a", "na", "-", "--", "—", "无", "暂无", "未提及", "未明确", "未知", "待定", "待确认"}
)

# 优先级取值的中英文写法映射（仅做归一化，不做推断）
_PRIORITY_ALIASES: dict[str, str] = {
    "high": "high",
    "高": "high",
    "h": "high",
    "medium": "medium",
    "中": "medium",
    "m": "medium",
    "low": "low",
    "低": "low",
    "l": "low",
}

# 状态取值的中英文写法映射（仅做归一化，不做推断）
#
# 重要：status 必须来自材料中的**明确表达**，不允许由标题、部门、优先级推测。
# 未匹配到任何明确写法时返回 None，由 normalize_tasks 落回 settings.DEFAULT_STATUS，
# 从而满足「材料没写状态就不要猜成已完成或进行中」的要求。
_STATUS_ALIASES: dict[str, str] = {
    "pending": "pending",
    "待办": "pending",
    "未开始": "pending",
    "processing": "processing",
    "进行中": "processing",
    "正在推进": "processing",
    " underway": "processing",
    "completed": "completed",
    "已完成": "completed",
    "已结束": "completed",
    "已落实": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "已取消": "cancelled",
    "已作废": "cancelled",
}

# 材料明确表示「已取消 / 已作废」时使用的标记。
#
# 该状态**不进入任务库**：已取消的事项没有后续执行动作，把它建成任务只会在
# 督办清单里制造一条永远不需要处理的噪声项，而项目也并未提供「已取消」这一
# 任务状态（settings.TASK_STATUSES 中没有该取值）。因此识别到取消语义时，
# 视为「不构成可执行事项」直接丢弃。
STATUS_DROPPED: str = "cancelled"

# 截止时间允许的写法：2026-09-30 / 2026/9/30 / 2026.9.30 / 2026年9月30日
_DEADLINE_RE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")

# 大模型调用函数签名：(system_prompt, user_prompt) -> JSON 对象
LlmFn = Callable[[str, str], Any]


def extract_tasks(
    text: str,
    source_type: str = "text",
    llm_fn: LlmFn | None = None,
    organization: str | None = None,
) -> list[dict[str, Any]]:
    """从文本中识别可执行事项。

    参数：
        text         待识别文本（Word/PDF/Excel 解析结果或手动输入）
        source_type  来源类型，取值见 settings.SOURCE_TYPES
        llm_fn       大模型调用函数，默认使用 llm.client.chat_json；
                     显式注入便于测试与后续替换模型
        organization 部门职责知识块；默认 None 表示自动读取
                     knowledge/organization/departments.json，
                     传 "" 可显式禁用知识库（测试对照用）
    返回：
        规范化后的任务列表；未识别到事项时返回空列表
    异常：
        ValueError        文本为空
        KnowledgeError    部门职责知识文件结构非法
        client.LLMError   模型调用失败或返回内容无法解析为 JSON
    """
    normalized = parser_tool.normalize_text(text)
    if not normalized:
        raise ValueError("待识别文本为空，无法提取事项。")

    # 来源类型做一次收敛：非法值按纯文本处理
    if source_type not in settings.SOURCE_TYPES:
        source_type = "text"

    # ---------- 结构化台账：走确定性逐行解析，不调用大模型 ----------
    # 表格已经把行边界与字段值写死了，再让模型判断一次「这行算不算任务、
    # 算完成还是待办」既无必要也不可复现（实测同一表格 5 次调用结果不同、
    # completed 行会整行丢失）。因此这里直接按行映射，识别失败则退回大模型路径。
    ledger_rows = ledger_parser.parse_ledger(normalized)
    if ledger_rows is not None:
        return normalize_tasks(ledger_rows)

    user_prompt = prompts.TASK_EXTRACTION_USER.format(
        source_type_label=SOURCE_TYPE_LABELS.get(source_type, "纯文本"),
        today=date.today().isoformat(),
        text=normalized,
    )

    system_prompt = prompts.build_task_extraction_system(_knowledge_block(organization))

    caller = llm_fn or client.chat_json
    payload = _coerce_payload(caller(system_prompt, user_prompt))
    return apply_directional_guard(normalized, normalize_tasks(payload))


def apply_directional_guard(source_text, tasks):
    """对大模型抽取结果做原文证据校验（P0-A / P0-B）。

    只作用于大模型路径：台账材料走 ledger_parser 确定性解析，在此前已 return。

    两条规则：
    1. 方向性剔除：标题若只在「未来工作概述」句里出现、且无独立具体出处，剔除。
    2. 责任取证：department / owner 必须在该事项所属那一句里出现，否则置 None。

    判定完全基于原文证据，不使用「缺 owner / 缺 deadline」作为剔除依据。
    找不到原文对应句时一律保留（宁可不删）。
    """
    # 注意：此处**不能**在 tasks 为空时提前返回。
    # LLM 完全漏抽恰恰是 Evidence Rescue 要补救的场景，
    # 提前返回会让补救层完全不执行。
    kept = []
    for task in tasks:
        title = str(task.get("title") or "")
        verdict = directional_guard.classify(title, source_text)
        if verdict["directional"]:
            continue
        evidence = str(verdict.get("evidence") or "")
        if evidence:
            for field in ("department", "owner"):
                value = task.get(field)
                if value and not directional_guard.has_field_evidence(evidence, value):
                    task[field] = None
        kept.append(task)
    return rescue.merge(kept, rescue.find_candidates(source_text))


def _knowledge_block(organization: str | None) -> str:
    """取得注入 System Prompt 的部门职责知识块。

    - organization 为 None：从知识库自动加载，文件缺失时返回空串（回退基础 Prompt）；
    - organization 为字符串：原样使用（传 "" 即禁用知识库）。
    知识文件结构非法时抛 KnowledgeError，不带病运行。
    """
    if organization is not None:
        return organization
    return organization_block()


def normalize_tasks(payload: Any) -> list[dict[str, Any]]:
    """把模型返回的原始结构规范化为统一的任务列表。

    容错范围：
    - payload 为 dict：支持 {"tasks": [...]} 包装形式或单个任务对象；
    - 标题字段兼容早期的 "task" 写法；
    - title 为空的任务会被丢弃（无标题不构成可执行事项）；
    - **材料明确表示「已取消 / 已作废」的事项会被丢弃**（无后续执行动作，
      且项目未提供该任务状态，建库只会在督办清单里留下噪声）；
    - 状态按材料中的明确表达归一化；未明确表达时落回默认待办，**不猜测**。

    状态口径（v2.0 修复）：
        此前本函数把 status 写死为 pending，导致「工作进展 / 完成情况汇报」里
        已完成的历史事项被强制变成「待办」，凭空制造出材料中并不存在的执行任务。
        现改为按材料明确表达取值：completed / processing / pending，
        cancelled 丢弃，缺失或非法落回 pending。
    """
    items = _as_items(payload)
    tasks: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        # 标题兼容 "title" 与 "task" 两种写法
        title = _clean_text(item.get("title")) or _clean_text(item.get("task"))
        if not title:
            continue

        # 材料明确表示已取消 / 已作废 → 不构成可执行事项，直接丢弃
        if _normalize_status(item.get("status")) == STATUS_DROPPED:
            continue

        tasks.append(
            {
                "title": title,
                "department": _clean_text(item.get("department")),
                "owner": _clean_text(item.get("owner")),
                "deadline": _normalize_deadline(item.get("deadline")),
                "priority": _normalize_priority(item.get("priority")),
                # 状态取自材料的明确表达；无明确表达则用默认待办，不猜成已完成/进行中
                "status": _normalize_status(item.get("status")) or settings.DEFAULT_STATUS,
                "description": _clean_text(item.get("description")),
            }
        )

    return tasks


# ------------------------------------------------------------
# 内部工具函数
# ------------------------------------------------------------


def _coerce_payload(result: Any) -> Any:
    """把 llm_fn 的返回值统一成「可迭代的原始结构」。

    支持三种情况：
    1. 已经是 list / dict（chat_json 的常规返回）；
    2. 字符串（模型返回带围栏或前排文字的 JSON），交给 client.extract_json 解析；
    3. 其他类型直接报错，避免脏数据流入后续流程。
    """
    if isinstance(result, str):
        return client.extract_json(result)
    if isinstance(result, (list, dict)):
        return result
    raise ValueError(f"模型返回类型不受支持：{type(result).__name__}")


def _as_items(payload: Any) -> list[Any]:
    """把 payload 统一为元素列表。"""
    if isinstance(payload, dict):
        # 支持 {"tasks": [...]} 包装
        wrapped = payload.get("tasks") or payload.get("items")
        if isinstance(wrapped, list):
            return wrapped
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError(f"无法从模型返回中解析任务列表：{type(payload).__name__}")


def _clean_text(value: Any) -> str | None:
    """清洗文本字段：去空白、去包裹引号，把「无/未提及」等写法统一转为 None。"""
    if value is None:
        return None
    text = str(value).strip().strip("\"'").strip()
    if text.lower() in _NULL_TOKENS:
        return None
    return text


def _normalize_priority(value: Any) -> str | None:
    """归一化优先级；非法或缺失返回 None（禁止猜测，交由 PlannerAgent 兜底）。"""
    text = _clean_text(value)
    if text is None:
        return None
    return _PRIORITY_ALIASES.get(text.lower())


def _normalize_status(value: Any) -> str | None:
    """归一化状态；材料未明确表达或写法无法识别时返回 None（交由调用方落回默认待办）。

    只做「写法归一化」，绝不由标题、部门、优先级等字段反推状态。
    """
    text = _clean_text(value)
    if text is None:
        return None
    return _STATUS_ALIASES.get(text.lower())


def _normalize_deadline(value: Any) -> str | None:
    """归一化截止时间为 YYYY-MM-DD。

    只做格式转换，不做日期推断：无法解析为具体日期时返回 None，
    因此「尽快」「近期」等表述会被视为缺失。
    """
    text = _clean_text(value)
    if text is None:
        return None

    matched = _DEADLINE_RE.search(text)
    if not matched:
        return None

    year, month, day = (int(part) for part in matched.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:  # 例如 2026-02-30
        return None
