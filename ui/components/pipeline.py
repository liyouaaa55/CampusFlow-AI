"""识别流水线的界面编排层。

只负责「按顺序调用已有业务函数 + 准备演示素材」，不新增、不修改任何业务规则：
    task_agent.extract_tasks()  事项识别（保留原有 Prompt、模型与容错逻辑）
    planner_agent.plan()        优先级 / 风险等级 / 人工确认标记

页面拿到的结果与直接调用上述函数完全一致。
"""

from __future__ import annotations

from typing import Any, Callable

from agent import planner_agent, task_agent
from knowledge import loader as knowledge_loader

# 一键体验演示使用的内置文档（已存在于知识库模板中）
DEMO_TEMPLATE_NAME: str = "meeting_minutes.md"
DEMO_FILE_LABEL: str = "内置会议纪要模板.md"

# 一键体验演示使用的来源类型（与 upload_page 的选项一致）
DEMO_SOURCE_TYPE: str = "meeting"

# 可注入的模型调用函数签名（供测试替换，生产环境为空）
LlmFn = Callable[[str, str], Any]


def identify_and_plan(
    text: str,
    source_type: str = "text",
    llm_fn: LlmFn | None = None,
) -> list[dict[str, Any]]:
    """事项识别 + 智能规划，返回待确认的任务列表。

    异常与直接调用业务函数时一致：
        ValueError        文本为空
        KnowledgeError    知识库文件结构非法
        client.LLMError   模型调用失败或返回内容无法解析
    """
    tasks = task_agent.extract_tasks(text, source_type=source_type, llm_fn=llm_fn)
    return planner_agent.plan(tasks)


def load_demo_document() -> str:
    """读取内置会议纪要模板，作为「一键体验演示」的输入文本。

    读取失败或文件为空时返回空字符串，由调用方给出提示。
    """
    return knowledge_loader.load_template_text(DEMO_TEMPLATE_NAME).strip()
