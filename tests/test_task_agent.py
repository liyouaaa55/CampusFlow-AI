"""TaskAgent 单元测试。

测试策略：**不调用真实 DeepSeek API**。
通过 extract_tasks 的 llm_fn 参数注入「假模型」，从而稳定地验证：
Prompt 组装、JSON 容错解析、字段规范化、缺失字段处理等本阶段真正实现的逻辑。
所有文本均为演示数据（Synthetic Data）。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from agent import task_agent
from config import settings
from llm import client, prompts
from tools import parser_tool

# ============================================================
# 测试数据（演示数据 Synthetic Data）
# ============================================================

# 正常会议纪要片段
MEETING_TEXT = """2026 年秋季学期第 3 次学生工作例会纪要（演示数据 Synthetic Data）
一、安全工作：下周完成宿舍消防隐患专项排查，由保卫处王老师牵头，务必在 2026-09-30 前完成整改台账。
二、活动安排：举办智能机器人创新周，学工办张老师负责制定活动实施方案，截止 2026-10-15。
三、会议要求：各科室加强宣传，及时报送简报。
"""


def make_fake_llm(payload: Any) -> Any:
    """构造假模型：记录收到的 Prompt，并返回预置结果。

    返回值结构与 llm.client.chat_json 一致（dict / list / str）。
    """
    received: dict[str, str] = {}

    def fake_llm(system_prompt: str, user_prompt: str) -> Any:
        received["system"] = system_prompt
        received["user"] = user_prompt
        return payload

    fake_llm.received = received  # type: ignore[attr-defined]
    return fake_llm


# ============================================================
# 场景一：正常会议
# ============================================================


def test_extract_normal_meeting() -> None:
    """正常会议纪要：三个事项均被规范化，字段齐全且格式正确。"""
    payload = [
        {
            "title": "完成宿舍消防隐患专项排查",
            "department": "保卫处",
            "owner": "王老师",
            "deadline": "2026-09-30",
            "priority": "high",
            "status": "pending",
            "description": "建立整改台账并完成隐患销号",
        },
        {
            "title": "举办智能机器人创新周",
            "department": "学工办",
            "owner": "张老师",
            "deadline": "2026/10/15",  # 斜杠写法应被归一化
            "priority": "medium",
            "status": "pending",
            "description": "负责制定活动实施方案",
        },
        {
            "title": "报送工作简报",
            "department": None,
            "owner": None,
            "deadline": None,
            "priority": "low",
            "status": "pending",
            "description": None,
        },
    ]

    tasks = task_agent.extract_tasks(MEETING_TEXT, source_type="meeting", llm_fn=make_fake_llm(payload))

    assert len(tasks) == 3
    assert tasks[0]["title"] == "完成宿舍消防隐患专项排查"
    assert tasks[0]["department"] == "保卫处"
    assert tasks[0]["owner"] == "王老师"
    assert tasks[0]["deadline"] == "2026-09-30"
    assert tasks[0]["priority"] == "high"

    # 斜杠日期归一化为 ISO 格式
    assert tasks[1]["deadline"] == "2026-10-15"

    # 输出字段顺序必须与设计文档一致，便于前端直接展示
    assert list(tasks[0].keys()) == list(task_agent.TASK_FIELDS)

    # 结果必须可被 json.dumps 序列化（禁止携带不可序列化对象）
    json.dumps(tasks, ensure_ascii=False)


def test_status_follows_material_expression() -> None:
    """状态取自材料的明确表达，而不是被强制写死为 pending。

    业务口径变更（v2.0 修复 P0-1 / P0-2）：
        此前本测试断言「状态恒为 pending」，把「汇报材料中的已完成事项被
        改写成待办」这个缺陷固化成了规范。修正后：材料明确写 completed
        就应当是 completed，否则历史完成事项会变成凭空新增的待办任务。
    """
    payload = [{"title": "整理实验室安全台账", "status": "completed", "priority": "high"}]
    tasks = task_agent.extract_tasks("实验室安全台账整理", llm_fn=make_fake_llm(payload))
    assert tasks[0]["status"] == "completed"


def test_status_defaults_to_pending_when_not_stated() -> None:
    """材料未明确表达状态时落回默认待办，且不猜测为已完成/进行中。"""
    payload = [{"title": "整理实验室安全台账", "priority": "high"}]
    tasks = task_agent.extract_tasks("实验室安全台账整理", llm_fn=make_fake_llm(payload))
    assert tasks[0]["status"] == "pending"


# ============================================================
# 场景二：负责人缺失
# ============================================================


def test_owner_missing_returns_none() -> None:
    """负责人缺失：返回 None，禁止猜测；「未明确」等写法同样视为缺失。"""
    payload = [
        {"title": "组织新生消防安全演练", "department": "保卫处", "owner": None},
        {"title": "更新宣传栏内容", "department": "宣传部", "owner": "未明确"},
        {"title": "梳理教学检查问题清单", "department": "教务处", "owner": "待定"},
    ]

    tasks = task_agent.extract_tasks("相关事项由各部门落实。", llm_fn=make_fake_llm(payload))

    assert len(tasks) == 3
    for task in tasks:
        assert task["owner"] is None
    # 部门信息正常保留
    assert tasks[0]["department"] == "保卫处"


# ============================================================
# 场景三：截止时间缺失
# ============================================================


@pytest.mark.parametrize("raw_deadline", [None, "", "待定", "尽快完成", "近期", "下周三", "2026-02-30"])
def test_deadline_missing_or_invalid_returns_none(raw_deadline: Any) -> None:
    """截止时间缺失或无法解析为具体日期时返回 None（不做日期推断）。"""
    payload = [{"title": "开展实验室安全检查", "deadline": raw_deadline}]
    tasks = task_agent.extract_tasks("开展实验室安全检查。", llm_fn=make_fake_llm(payload))
    assert tasks[0]["deadline"] is None


@pytest.mark.parametrize(
    ("raw_deadline", "expected"),
    [
        ("2026-09-30", "2026-09-30"),
        ("2026/9/30", "2026-09-30"),
        ("2026.9.30", "2026-09-30"),
        ("2026年9月30日", "2026-09-30"),
        ("截止时间：2026-10-08 下班前", "2026-10-08"),
    ],
)
def test_deadline_variants_normalized(raw_deadline: str, expected: str) -> None:
    """常见日期写法统一归一化为 YYYY-MM-DD。"""
    payload = [{"title": "提交活动总结", "deadline": raw_deadline}]
    tasks = task_agent.extract_tasks("提交活动总结。", llm_fn=make_fake_llm(payload))
    assert tasks[0]["deadline"] == expected


# ============================================================
# 场景四：多个任务
# ============================================================


def test_multiple_tasks_split_and_ordered() -> None:
    """多事项必须全部保留，且顺序与模型输出一致（会议原文顺序）。"""
    payload = [
        {"title": "完成消防隐患专项排查", "department": "保卫处", "priority": "high"},
        {"title": "举办智能机器人创新周", "department": "学工办", "priority": "medium"},
        {"title": "开展期初教学检查", "department": "教务处", "priority": "medium"},
        {"title": "更新校园宣传栏", "department": "宣传部", "priority": "medium"},
        {"title": "报送工作简报", "department": "党政办", "priority": "low"},
    ]

    tasks = task_agent.extract_tasks(MEETING_TEXT, llm_fn=make_fake_llm(payload))

    assert len(tasks) == 5
    assert [task["title"] for task in tasks] == [
        "完成消防隐患专项排查",
        "举办智能机器人创新周",
        "开展期初教学检查",
        "更新校园宣传栏",
        "报送工作简报",
    ]
    assert [task["priority"] for task in tasks] == ["high", "medium", "medium", "medium", "low"]


def test_payload_wrapped_in_tasks_key() -> None:
    """模型偶尔会返回 {"tasks": [...]} 包装形式，应被正常解析。"""
    payload = {"tasks": [{"title": "举办机器人创新周"}, {"title": "完成消防演练"}]}
    tasks = task_agent.extract_tasks(MEETING_TEXT, llm_fn=make_fake_llm(payload))
    assert [task["title"] for task in tasks] == ["举办机器人创新周", "完成消防演练"]


def test_empty_title_item_is_dropped() -> None:
    """无标题的记录不构成可执行事项，应被丢弃而不是生成空任务。"""
    payload = [{"title": "  ", "department": "学工办"}, {"title": "举办机器人创新周"}]
    tasks = task_agent.extract_tasks(MEETING_TEXT, llm_fn=make_fake_llm(payload))
    assert [task["title"] for task in tasks] == ["举办机器人创新周"]


def test_empty_payload_returns_empty_list() -> None:
    """文本中没有可执行事项时返回空列表。"""
    tasks = task_agent.extract_tasks("本次会议主要是通报情况，无具体安排。", llm_fn=make_fake_llm([]))
    assert tasks == []


# ============================================================
# 场景五：安全事项优先级
# ============================================================


def test_safety_item_priority_is_high() -> None:
    """安全类事项优先级为 high，且优先级关键词规则必须写在 System Prompt 中。"""
    payload = [
        {
            "title": "完成宿舍消防隐患专项排查",
            "department": "保卫处",
            "owner": "王老师",
            "deadline": "2026-09-30",
            "priority": "high",
            "status": "pending",
            "description": "建立整改台账",
        }
    ]
    fake_llm = make_fake_llm(payload)
    tasks = task_agent.extract_tasks(MEETING_TEXT, source_type="meeting", llm_fn=fake_llm)

    # 1. 管道不篡改模型给出的 high
    assert tasks[0]["priority"] == "high"

    # 2. 优先级规则已落入 System Prompt（本阶段规则的落点就是 Prompt）
    system_prompt = fake_llm.received["system"]
    for keyword in ("安全", "消防", "实验室", "应急"):
        assert keyword in system_prompt
    for keyword in ("教学", "活动", "宣传"):
        assert keyword in system_prompt
    assert "high" in system_prompt and "medium" in system_prompt and "low" in system_prompt

    # 3. 约束：只输出 JSON、缺失返回 null、禁止 Markdown
    assert "JSON" in system_prompt
    assert "null" in system_prompt
    assert "Markdown" in system_prompt


def test_priority_alias_normalized_and_invalid_becomes_none() -> None:
    """优先级别名做归一化；非法值置为 None，交由 PlannerAgent 兜底。"""
    payload = [
        {"title": "消防设施检修", "priority": "HIGH"},
        {"title": "举办运动会", "priority": "中"},
        {"title": "整理会议材料", "priority": "P0"},
        {"title": "报送简报", "priority": None},
    ]
    tasks = task_agent.extract_tasks(MEETING_TEXT, llm_fn=make_fake_llm(payload))

    assert [task["priority"] for task in tasks] == ["high", "medium", None, None]


# ============================================================
# 场景六：Prompt 组装与 JSON 容错
# ============================================================


def test_user_prompt_contains_source_type_today_and_text() -> None:
    """User Prompt 必须携带来源类型、当前日期与原文。"""
    fake_llm = make_fake_llm([{"title": "举办机器人创新周"}])
    task_agent.extract_tasks(MEETING_TEXT, source_type="meeting", llm_fn=fake_llm)

    user_prompt = fake_llm.received["user"]
    assert "会议纪要" in user_prompt
    assert date.today().isoformat() in user_prompt
    assert "举办智能机器人创新周" in user_prompt


def test_fenced_json_response_is_tolerated() -> None:
    """模型返回 ```json 围栏或前后带解释文字时，仍能正确解析。"""
    raw = (
        "好的，识别结果如下：\n```json\n"
        '[{"title": "完成消防隐患专项排查", "priority": "high"}]\n```\n以上。'
    )
    fake_llm = make_fake_llm(raw)
    tasks = task_agent.extract_tasks(MEETING_TEXT, llm_fn=fake_llm)

    assert len(tasks) == 1
    assert tasks[0]["title"] == "完成消防隐患专项排查"
    assert tasks[0]["priority"] == "high"


def test_non_json_response_raises_llm_error() -> None:
    """模型只返回自然语言时抛出 LLMError，而不是静默产生脏数据。"""
    fake_llm = make_fake_llm("已将会议内容整理完毕，共识别 3 项任务。")
    with pytest.raises(client.LLMError):
        task_agent.extract_tasks(MEETING_TEXT, llm_fn=fake_llm)


def test_unsupported_payload_type_raises_value_error() -> None:
    """模型返回类型异常（如数字）时立刻报错。"""
    fake_llm = make_fake_llm(123)
    with pytest.raises(ValueError):
        task_agent.extract_tasks(MEETING_TEXT, llm_fn=fake_llm)


def test_extract_json_handles_plain_text() -> None:
    """client.extract_json 对纯 JSON 文本、带围栏文本均可解析。"""
    assert client.extract_json('[{"title": "举办机器人创新周"}]')[0]["title"] == "举办机器人创新周"
    assert client.extract_json("```json\n{\"title\": \"完成消防排查\"}\n```")["title"] == "完成消防排查"


@pytest.mark.skipif(settings.is_llm_ready(), reason="已配置 DEEPSEEK_API_KEY，跳过密钥缺失校验")
def test_missing_api_key_raises_llm_error() -> None:
    """未配置密钥时，客户端应给出明确提示而非发起请求。"""
    with pytest.raises(client.LLMError, match="DEEPSEEK_API_KEY"):
        client.get_client()


def test_empty_text_raises_value_error() -> None:
    """空文本直接拒绝，不发起模型调用。"""
    with pytest.raises(ValueError):
        task_agent.extract_tasks("   \n\t  ", llm_fn=make_fake_llm([]))


def test_invalid_source_type_falls_back_to_text() -> None:
    """非法来源类型回退为纯文本，不影响识别流程。"""
    fake_llm = make_fake_llm([{"title": "整理档案"}])
    tasks = task_agent.extract_tasks(MEETING_TEXT, source_type="unknown-type", llm_fn=fake_llm)
    assert len(tasks) == 1
    assert "纯文本" in fake_llm.received["user"]


# ============================================================
# 场景七：文本预处理
# ============================================================


def test_normalize_text_collapses_noise() -> None:
    """文本预处理：统一全角空格、压缩行内空白、多行空行折叠为一个空行。"""
    messy = "第一行\u3000内容\r\n\r\n\r\n   第二行  内容   \n\n"
    # 段间保留一个空行（保持段落结构），行内连续空白被压缩
    assert parser_tool.normalize_text(messy) == "第一行 内容\n\n第二行 内容"


def test_detect_source_type_by_extension() -> None:
    """按扩展名推断来源类型（会议纪要需由调用方显式指定）。"""
    assert parser_tool.detect_source_type("会议纪要.docx") == "document"
    assert parser_tool.detect_source_type("任务台账.xlsx") == "excel"
    assert parser_tool.detect_source_type("说明.txt") == "text"
    assert parser_tool.detect_source_type("无扩展名文件") == "text"
