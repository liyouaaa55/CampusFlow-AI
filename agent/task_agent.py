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
- 缺失字段一律返回 None，不做任何猜测（与 Prompt 中的约束一致）；
- status 在识别阶段固定为 pending，由后续 PlannerAgent / 人工确认推进；
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
from tools import parser_tool

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

    user_prompt = prompts.TASK_EXTRACTION_USER.format(
        source_type_label=SOURCE_TYPE_LABELS.get(source_type, "纯文本"),
        today=date.today().isoformat(),
        text=normalized,
    )

    system_prompt = prompts.build_task_extraction_system(_knowledge_block(organization))

    caller = llm_fn or client.chat_json
    payload = _coerce_payload(caller(system_prompt, user_prompt))
    return normalize_tasks(payload)


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
    - 缺失 / 非法字段统一置为 None 或默认状态，不猜测。
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

        tasks.append(
            {
                "title": title,
                "department": _clean_text(item.get("department")),
                "owner": _clean_text(item.get("owner")),
                "deadline": _normalize_deadline(item.get("deadline")),
                "priority": _normalize_priority(item.get("priority")),
                # 识别阶段状态固定为 pending
                "status": settings.DEFAULT_STATUS,
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
