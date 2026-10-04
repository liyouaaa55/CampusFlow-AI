"""页面二：会议材料上传。

流程（全部复用已有业务函数，不新增业务逻辑）：
    上传文件 → tools.parser_tool.parse_file()
             → pipeline.identify_and_plan()（TaskAgent + PlannerAgent）
结果暂存 session_state，**不入库**（入库需到「事项智能识别」页人工确认）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from config import settings
from knowledge import KnowledgeError
from llm import client
from tools import parser_tool
from ui.components import cards, pipeline, state

# 页面标题与副标题
PAGE_TITLE: str = "会议材料上传"
PAGE_SUBTITLE: str = "上传会议纪要、公文或任务台账，系统自动识别可执行事项。"

# 允许上传的扩展名（与 parser_tool 支持范围一致）
ALLOWED_TYPES: list[str] = ["pdf", "docx", "txt", "md", "xlsx", "xls", "csv"]

# 单文件大小上限（MB），超限时在界面直接拦截，避免上传后解析失败
MAX_UPLOAD_MB: int = 20

# 来源类型选项：界面显示中文，传给 Agent 用英文枚举
SOURCE_TYPE_OPTIONS: dict[str, str] = {
    "会议纪要": "meeting",
    "公文通知": "document",
    "Excel台账": "excel",
    "纯文本": "text",
}

# 解析文本预览的最大字符数
PREVIEW_LIMIT: int = 3000

# 三步流程条（与页面跳转链路一致：上传 → 识别 → 入库）
FLOW_STEPS: tuple[str, ...] = ("上传材料", "AI识别事项", "确认入库")


def render() -> None:
    """渲染会议材料上传页面。"""
    cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)
    cards.steps(FLOW_STEPS, _current_step())

    uploaded = st.file_uploader("上传文件", type=ALLOWED_TYPES, label_visibility="collapsed")
    source_label = st.radio("来源类型", list(SOURCE_TYPE_OPTIONS), horizontal=True)

    oversized = _render_file_card(uploaded, source_label)

    if st.button("开始智能解析", type="primary", disabled=uploaded is None or oversized):
        _run_pipeline(uploaded, SOURCE_TYPE_OPTIONS[source_label])

    if not settings.is_llm_ready():
        st.warning("未检测到模型密钥，智能识别暂不可用。请配置 `.env` 后重启应用。")

    _render_pending()

    st.write("")
    cards.hint(["支持：会议纪要、公文通知、Excel台账、TXT事务记录"])
    cards.note("演示数据（Synthetic Data），仅用于比赛展示。")


# ------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------


def _current_step() -> int:
    """当前所处步骤：已有待确认结果时进入第 2 步，否则停在第 1 步。"""
    return 1 if state.has_parsed() else 0


def _render_file_card(uploaded: Any, source_label: str) -> bool:
    """渲染文件信息卡，返回文件是否超出大小限制。"""
    if uploaded is None:
        return False

    oversized = int(uploaded.size) > MAX_UPLOAD_MB * 1024 * 1024
    if oversized:
        cards.file_card(uploaded.name, _format_size(uploaded.size), source_label, "文件过大，无法解析")
        st.error(f"单个文件不得超过 {MAX_UPLOAD_MB}MB，请压缩或拆分后重新上传。")
        return True

    cards.file_card(uploaded.name, _format_size(uploaded.size), source_label, "等待智能解析")
    return False


def _run_pipeline(uploaded: Any, source_type: str) -> None:
    """执行「保存 → 解析 → 识别」三步流水线，结果写入 session_state。"""
    try:
        with st.status("正在执行智能解析…", expanded=True) as status:
            saved_path = _save_upload(uploaded)
            st.write(f"① 保存文件：{saved_path.name}")

            text = parser_tool.parse_file(saved_path)
            if not text.strip():
                raise ValueError("未能从文件中提取到任何文本内容。")
            st.write(f"② 文本解析：共 {len(text)} 个字符")

            planned = pipeline.identify_and_plan(text, source_type=source_type)
            st.write(f"③ 智能事项识别：识别到 {len(planned)} 项事项")

            status.update(label=f"解析完成，共识别 {len(planned)} 项事项", state="complete")
    except client.LLMError as exc:
        st.error(f"调用大模型失败：{exc}")
        return
    except (ValueError, FileNotFoundError, ImportError, KnowledgeError) as exc:
        st.error(f"解析失败：{exc}")
        return

    state.save_parsed(planned, uploaded.name, text)
    st.success(f"成功识别 {len(planned)} 项事项，即将进入事项智能识别页面。")
    if st.button("进入事项智能识别", type="primary"):
        state.goto("事项智能识别")


def _save_upload(uploaded: Any) -> Path:
    """把上传的文件写入 data/uploads，返回落盘路径。"""
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 只取文件名部分，避免路径穿越
    filename = Path(uploaded.name).name
    target = settings.UPLOAD_DIR / f"{datetime.now():%Y%m%d%H%M%S}_{filename}"
    target.write_bytes(uploaded.getbuffer())
    return target


def _render_pending() -> None:
    """展示暂存的识别结果入口（尚未入库）。"""
    if not state.has_parsed():
        return

    tasks = state.get_parsed_tasks()
    st.divider()
    cards.section("当前待确认结果", "尚未入库，需人工确认")
    st.caption(f"来源文件：{state.get_parsed_file()}｜待确认条数：{len(tasks)}")

    if st.button("进入事项智能识别"):
        state.goto("事项智能识别")

    with st.expander("查看解析文本预览", expanded=False):
        st.text(state.get_parsed_text()[:PREVIEW_LIMIT])


def _format_size(size: int) -> str:
    """文件大小友好展示。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"
