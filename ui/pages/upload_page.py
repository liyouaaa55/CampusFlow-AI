"""页面二：会议材料上传。

流程（全部复用已有业务函数，不新增业务逻辑）：
    上传文件 → tools.parser_tool.parse_file()
             → pipeline.identify_and_plan()（TaskAgent + PlannerAgent）
结果暂存 session_state，**不入库**（入库需到「事项智能识别」页人工确认）。
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Mapping

import streamlit as st

from config import settings
from knowledge import KnowledgeError
from llm import client
from tools import parser_tool
from ui.components import cards, pipeline, state, styles

# 页面标题与副标题
PAGE_TITLE: str = "会议材料上传"
PAGE_SUBTITLE: str = "上传会议纪要、公文或任务台账，系统自动识别可执行事项。"

# 允许上传的扩展名（与 parser_tool 支持范围一致）
ALLOWED_TYPES: list[str] = ["pdf", "docx", "txt", "md", "xlsx", "xls", "csv"]

# st.status 作用域标记定义在 styles.STATUS_MARKER（共享常量，避免页面间相互导入）

# 单文件大小上限（MB），超限时在界面直接拦截，避免上传后解析失败
MAX_UPLOAD_MB: int = 20

# 解析进度三行（①②③）的统一样式：15px 字号、深灰蓝正文色、上下 6px 间距
STEP_CSS: str = "font-size: 15px; color: #334155; margin: 6px 0;"

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

# 「载入示例材料」区块
SAMPLE_SECTION: str = "没有合适的文件？载入示例材料"
SAMPLE_CAPTION: str = "示例均为虚构演示数据，用于验证系统能力，不模仿任何真实学校文件"
BUTTON_LOAD_SAMPLE: str = "载入并解析"

# 支持格式的对外说明（与 ALLOWED_TYPES 保持一致，避免界面承诺与实际能力不符）
SUPPORT_HINT: str = "支持格式：PDF、Word（.docx）、Excel（.xlsx / .xls / .csv）、TXT、Markdown"


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

    st.write("")
    _render_sample_loader()

    _render_pending()

    st.write("")
    cards.hint([SUPPORT_HINT])
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
            st.markdown(styles.STATUS_MARKER, unsafe_allow_html=True)
            saved_path = _save_upload(uploaded)
            # ①②③ 三步进度改用 HTML 渲染：统一 15px / #334155 / 上下 6px 间距。
            # 三处内容全部由变量动态填充，换任何文件都会自动更新对应名称与数字。
            st.markdown(f'<div style="{STEP_CSS}">① 保存文件：{escape(saved_path.name)}</div>', unsafe_allow_html=True)

            text = parser_tool.parse_file(saved_path)
            if not text.strip():
                raise ValueError("未能从文件中提取到任何文本内容。")
            st.markdown(f'<div style="{STEP_CSS}">② 文本解析：共 {len(text)} 个字符</div>', unsafe_allow_html=True)

            planned = pipeline.identify_and_plan(text, source_type=source_type)
            st.markdown(f'<div style="{STEP_CSS}">③ 智能事项识别：识别到 {len(planned)} 项事项</div>', unsafe_allow_html=True)

            status.update(label="解析完成", state="complete")
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


def _render_sample_loader() -> None:
    """示例材料入口：手头没有合适文件时，也能完整走通核心流程。

    刻意复用上方同一条解析链路（parse_file → identify_and_plan），
    不新增任何业务规则，也不绕过解析器直接喂文本——
    这样示例材料才真的验证了解析能力。
    """
    cards.section(SAMPLE_SECTION, SAMPLE_CAPTION)

    options = {item["label"]: item for item in pipeline.SAMPLE_DOCUMENTS}
    picked = st.selectbox("选择示例材料", list(options), label_visibility="collapsed")
    item = options[picked]

    if st.button(BUTTON_LOAD_SAMPLE, type="primary"):
        _run_sample(item)


def _run_sample(item: Mapping[str, Any]) -> None:
    """载入并解析一份示例材料，结果同样写入 session_state 等待人工确认。"""
    key = str(item.get("key") or "")
    source_type = str(item.get("source_type") or "text")

    try:
        with st.status("正在载入示例材料…", expanded=True) as status:
            st.markdown(styles.STATUS_MARKER, unsafe_allow_html=True)
            st.write(f"① 读取示例文件：{key}")
            text = pipeline.load_sample_document(key)
            if not text.strip():
                raise ValueError("示例材料未解析出任何文本内容。")
            st.write(f"② 文本解析：共 {len(text)} 个字符")

            planned = pipeline.identify_and_plan(text, source_type=source_type)
            st.write(f"③ 智能事项识别：识别到 {len(planned)} 项事项")

            if planned:
                status.update(label="解析完成", state="complete")
            else:
                status.update(label="解析完成：该材料中无可执行事项", state="complete")
    except client.LLMError as exc:
        st.error(f"调用大模型失败：{exc}")
        return
    except (ValueError, FileNotFoundError, ImportError, KnowledgeError) as exc:
        st.error(f"解析失败：{exc}")
        return

    label = str(item.get("label") or key)
    state.save_parsed(planned, label, text)
    if planned:
        st.success(f"成功识别 {len(planned)} 项事项，即将进入事项智能识别页面。")
    else:
        st.info("该示例材料中未识别到可执行事项，这正是「不编造」的预期表现。")
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
