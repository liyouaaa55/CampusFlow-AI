"""轻量知识库包（非 RAG）。

知识库仅包含三类静态资料，全部以文件形式存放，按需读取、按需注入：
- organization/  部门职责   → 注入 TaskAgent 的 System Prompt，用于对齐部门名称
- rules/         规则        → priority.json 供 PlannerAgent，reminder.json 供 ReminderAgent
- templates/     模板        → 会议纪要 / 任务 / 报告结构

对外统一通过 knowledge.loader 访问。
"""

from __future__ import annotations

from knowledge.loader import (
    KnowledgeError,
    clear_cache,
    load_departments,
    load_template_json,
    load_template_text,
    organization_block,
)

__all__ = [
    "KnowledgeError",
    "clear_cache",
    "load_departments",
    "load_template_json",
    "load_template_text",
    "organization_block",
]
