"""页面二：会议材料与任务生成（v3.0 第一阶段）。

一页完成前半段完整链路，不再要求用户在多个页面之间往返跳转：
    上传材料 → 解析材料 → AI 识别事项 → 管理者修正 → 确认生成任务

分工（全部复用已有业务函数，不新增业务规则）：
    tools.parser_tool.parse_file()   文件 → 纯文本
    pipeline.identify_and_plan()     TaskAgent 识别 + PlannerAgent 规划
    review_page.render_inline()      可编辑结果 + 确认后 sqlite_db.batch_create() 入库

识别结果只暂存在 session_state，**未确认前绝不入库**；界面以「来源文件」明确标注
它来自哪一份材料，避免与正式任务记录混淆。
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
from ui.pages import review_page

# 页面标题与副标题
PAGE_TITLE: str = "会议材料与任务生成"
PAGE_SUBTITLE: str = "上传材料，AI 识别工作事项，管理者核对修正后确认生成任务。"

# 允许上传的扩展名（与 parser_tool 支持范围一致）
ALLOWED_TYPES: list[str] = ["pdf", "docx", "txt", "md", "xlsx", "xls", "csv"]

# st.status 作用域标记定义在 styles.STATUS_MARKER（共享常量，避免页面间相互导入）

# 单文件大小上限（MB），超限时在界面直接拦截，避免上传后解析失败
MAX_UPLOAD_MB: int = 20

# 解析进度三步（①②③）的统一样式由 styles.APP_CSS 中的
# .cf-parse-steps / .cf-parse-step / .cf-parse-idx / .cf-parse-title /
# .cf-parse-content 控制（序号加粗、标题与内容分层、轻分隔线）。
# 三行共用一个 st.empty 容器渲染（见 _steps_html），行距不受 Streamlit
# 相邻元素默认 gap 影响，视觉紧凑且分层清晰。


def _steps_html(steps: list[tuple[str, str, str]]) -> str:
    """渲染解析进度三步：序号加粗、标题与内容分层、轻分隔线。

    steps 每项 = (序号, 标题, 内容)，例如 ("①", "保存文件", "xxx.pdf")。
    文本统一在这里做 HTML 转义，调用方只传原始值即可。
    """
    rows = "".join(
        f'<div class="cf-parse-step">'
        f'<span class="cf-parse-idx">{escape(idx)}</span>'
        f'<span class="cf-parse-title">{escape(title)}</span>'
        f'<span class="cf-parse-content">{escape(content)}</span>'
        f"</div>"
        for idx, title, content in steps
    )
    return f'<div class="cf-parse-steps">{rows}</div>'

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
    # 标题 + 步骤条包进容器并打 header 标记：styles.APP_CSS 据此把
    # 标题 / 副标题 / 步骤条左内缩 26px，与下方卡片内容区对齐（统一左边界），
    # 宽度均为相对值，窗口缩放自动适配、不横向溢出。
    with st.container():
        st.markdown('<div data-cf-align="header" style="display:none"></div>', unsafe_allow_html=True)
        cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)
        cards.steps(FLOW_STEPS, _current_step())

    # 上传操作区整体包进白色卡片，统一卡片式布局。
    # 用隐藏标记 data-cf-card="upload" 供 styles.APP_CSS 的 :has() 锚定
    # （Streamlit 1.37 的 st.container 不支持 key 参数）。
    with st.container():
        st.markdown('<div data-cf-card="upload" style="display:none"></div>', unsafe_allow_html=True)
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
    # 底部提示区包进容器并打上 footer 标记：styles.APP_CSS 据此把
    # 提示条与说明文字左右内缩 26px，与上方卡片内容区对齐（三者同宽）。
    with st.container():
        st.markdown('<div data-cf-align="footer" style="display:none"></div>', unsafe_allow_html=True)
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
        # expanded=False：从创建起就收起，只占标题一行。
        # 不用「先展开再 update(expanded=False)」——实测 Streamlit 1.37 中
        # update 的 expanded 参数收不住前端 details，完成后仍是大块展开。
        # 解析过程中想看进度，点标题即可手动展开。
        with st.status("正在执行智能解析…", expanded=False) as status:
            st.markdown(styles.STATUS_MARKER, unsafe_allow_html=True)
            st.markdown(styles.STATUS_ICON_CSS, unsafe_allow_html=True)
            # 三步进度共用一个 st.empty 容器，每完成一步整体重写：
            # 行距不受 Streamlit 相邻元素默认 gap 影响，视觉紧凑。
            steps = st.empty()
            done: list[tuple[str, str, str]] = []
            saved_path = _save_upload(uploaded)
            done.append(("①", "保存文件", saved_path.name))
            steps.markdown(_steps_html(done), unsafe_allow_html=True)

            text = parser_tool.parse_file(saved_path)
            if not text.strip():
                raise ValueError("未能从文件中提取到任何文本内容。")
            done.append(("②", "文本解析", f"共 {len(text)} 个字符"))
            steps.markdown(_steps_html(done), unsafe_allow_html=True)

            planned = pipeline.identify_and_plan(text, source_type=source_type)
            done.append(("③", "智能事项识别", f"识别到 {len(planned)} 项事项"))
            steps.markdown(_steps_html(done), unsafe_allow_html=True)

            status.update(label="解析完成", state="complete", expanded=False)
    except client.LLMError as exc:
        st.error(f"调用大模型失败：{exc}")
        return
    except (ValueError, FileNotFoundError, ImportError, KnowledgeError) as exc:
        st.error(f"解析失败：{exc}")
        return
    except Exception:
        # 兜底：任何未预期的解析异常都转成中文提示，绝不把 traceback
        # （可能含本机文件路径）直接显示到页面上，也不产生半成品状态。
        st.error("文件解析失败：该文件可能已损坏或格式不受支持，请更换文件后重试。")
        return

    # 进度条完成即收起（只占标题一行），下方「当前待确认结果」统一提供
    # 入口与按钮，避免两块重复展示、占用过大纵向空间。
    state.save_parsed(planned, uploaded.name, text)


def _render_sample_loader() -> None:
    """示例材料入口：手头没有合适文件时，也能完整走通核心流程。

    刻意复用上方同一条解析链路（parse_file → identify_and_plan），
    不新增任何业务规则，也不绕过解析器直接喂文本——
    这样示例材料才真的验证了解析能力。
    """
    # 示例材料区整体包进白色卡片，与上传/结果卡片视觉统一。
    with st.container():
        st.markdown('<div data-cf-card="sample" style="display:none"></div>', unsafe_allow_html=True)
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
        with st.status("正在载入示例材料…", expanded=False) as status:
            st.markdown(styles.STATUS_MARKER, unsafe_allow_html=True)
            st.markdown(styles.STATUS_ICON_CSS, unsafe_allow_html=True)
            # 与 _run_pipeline 一致：三步进度共用一个容器，行距自控。
            steps = st.empty()
            done: list[tuple[str, str, str]] = []
            done.append(("①", "读取示例文件", key))
            steps.markdown(_steps_html(done), unsafe_allow_html=True)
            text = pipeline.load_sample_document(key)
            if not text.strip():
                raise ValueError("示例材料未解析出任何文本内容。")
            done.append(("②", "文本解析", f"共 {len(text)} 个字符"))
            steps.markdown(_steps_html(done), unsafe_allow_html=True)

            planned = pipeline.identify_and_plan(text, source_type=source_type)
            done.append(("③", "智能事项识别", f"识别到 {len(planned)} 项事项"))
            steps.markdown(_steps_html(done), unsafe_allow_html=True)

            if planned:
                status.update(label="解析完成", state="complete", expanded=False)
            else:
                status.update(label="解析完成：该材料中无可执行事项", state="complete", expanded=False)
    except client.LLMError as exc:
        st.error(f"调用大模型失败：{exc}")
        return
    except (ValueError, FileNotFoundError, ImportError, KnowledgeError) as exc:
        st.error(f"解析失败：{exc}")
        return
    except Exception:
        st.error("示例材料解析失败：文件可能已损坏或格式不受支持。")
        return

    label = str(item.get("label") or key)
    state.save_parsed(planned, label, text)
    # 识别到 0 项时给出明确解释（下方「当前待确认结果」区会显示条数为 0），
    # 不再重复成功提示与按钮，避免与 _render_pending 内容重叠、占用空间。
    if not planned:
        st.info("该示例材料中未识别到可执行事项，这正是「不编造」的预期表现。")


def _save_upload(uploaded: Any) -> Path:
    """把上传的文件写入 data/uploads，返回落盘路径。"""
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 只取文件名部分，避免路径穿越
    filename = Path(uploaded.name).name
    target = settings.UPLOAD_DIR / f"{datetime.now():%Y%m%d%H%M%S}_{filename}"
    target.write_bytes(uploaded.getbuffer())
    return target


def _render_pending() -> None:
    """当前材料的识别结果与确认入库（内联在本页，尚未入库）。"""
    if not state.has_parsed():
        return

    tasks = state.get_parsed_tasks()
    # 结果区整体包进白色卡片，与上传区视觉统一。
    with st.container():
        st.markdown('<div data-cf-card="pending" style="display:none"></div>', unsafe_allow_html=True)
        cards.section(
            "当前材料的识别结果（待确认）",
            "核对修正后点击「确认入库」才会写入任务数据库；未确认前不进入正式任务池",
        )
        st.caption(
            f"来源文件：{state.get_parsed_file()}｜识别条数：{len(tasks)}｜"
            "以下为待确认结果，尚未入库"
        )

        review_page.render_inline(tasks)

        with st.expander("查看解析文本预览", expanded=False):
            st.text(state.get_parsed_text()[:PREVIEW_LIMIT])


def _format_size(size: int) -> str:
    """文件大小友好展示。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"
