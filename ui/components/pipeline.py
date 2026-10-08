"""识别流水线的界面编排层。

只负责「按顺序调用已有业务函数 + 准备演示素材」，不新增、不修改任何业务规则：
    task_agent.extract_tasks()  事项识别（保留原有 Prompt、模型与容错逻辑）
    planner_agent.plan()        优先级 / 风险等级 / 人工确认标记

页面拿到的结果与直接调用上述函数完全一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent import planner_agent, task_agent
from config import settings
from knowledge import loader as knowledge_loader
from tools import parser_tool

# 一键体验演示使用的内置文档（已存在于知识库模板中）
DEMO_TEMPLATE_NAME: str = "meeting_minutes.md"
DEMO_FILE_LABEL: str = "内置会议纪要模板.md"

# 一键体验演示使用的来源类型（与 upload_page 的选项一致）
DEMO_SOURCE_TYPE: str = "meeting"

# 可注入的模型调用函数签名（供测试替换，生产环境为空）
LlmFn = Callable[[str, str], Any]


# ============================================================
# 示例材料（供上传页「载入示例材料」使用）
# ============================================================

# 每份示例材料覆盖一种核心能力，供功能验证使用。
# 全部为虚构演示数据（Synthetic Data），不模仿任何真实学校文件。
SAMPLE_DOCUMENTS: tuple[dict[str, str], ...] = (
    {
        "key": "示例_多事项会议纪要.md",
        "label": "多事项会议纪要（6 项 · 跨部门 · 含缺失字段）",
        "source_type": "meeting",
    },
    {
        "key": "示例_任务台账.xlsx",
        "label": "Excel 任务台账（5 项 · 含空缺单元格）",
        "source_type": "excel",
    },
    {
        "key": "示例_信息不完整的通知.md",
        "label": "信息不完整的通知（无负责人 · 无截止时间）",
        "source_type": "document",
    },
    {
        "key": "示例_无执行事项公告.md",
        "label": "无可执行事项的公告（应识别为 0 项）",
        "source_type": "document",
    },
)


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


def load_sample_document(key: str) -> str:
    """按文件名读取一份示例材料，返回其纯文本内容。

    刻意复用 parser_tool.parse_file：这样 .xlsx 示例与用户亲手上传的表格
    走**完全相同**的解析代码路径，示例材料因此可以真正验证解析能力，
    而不只是绕过解析器直接喂文本。

    异常：
        FileNotFoundError  示例文件不存在；
        ValueError         格式不支持或内容无法解析。
    """
    filename = Path(key).name
    target = settings.TEMPLATES_DIR / filename
    if not target.exists():
        raise FileNotFoundError(f"示例材料不存在：{filename}")
    return parser_tool.parse_file(target)
