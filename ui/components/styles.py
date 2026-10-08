"""V1.0 视觉规范：政务蓝白配色、CSS 注入与颜色/徽章工具。

说明：本模块只负责「视觉」，不含任何业务判断与数据访问。
所有色值与圆角集中在此处声明，页面与组件只引用常量，禁止在页面里写死颜色。
"""

from __future__ import annotations

import streamlit as st

# ============================================================
# 一、色板（政务蓝白）
# ============================================================

PRIMARY: str = "#2563EB"        # 主色：政务蓝
SUCCESS: str = "#16A34A"        # 正常
WARNING: str = "#F59E0B"        # 临期 / 需确认
URGENT: str = "#EA580C"         # 紧急
DANGER: str = "#DC2626"         # 逾期 / 高风险
BACKGROUND: str = "#F8FAFC"     # 页面背景
SURFACE: str = "#FFFFFF"        # 卡片底色
BORDER: str = "#E2E8F0"         # 描边
TEXT_MAIN: str = "#0F172A"      # 正文主色
TEXT_SUB: str = "#64748B"       # 次要文字 / 副标题
TEXT_MUTED: str = "#94A3B8"     # 最弱一级文字

# 统一圆角：卡片 12px，控件 10px
CARD_RADIUS: str = "12px"
RADIUS: str = "10px"

# 浅阴影（卡片）
SHADOW: str = "0 1px 3px rgba(15, 23, 42, 0.05)"

# 图表主色（保留旧名称，供柱状图引用）
BRAND_BLUE: str = PRIMARY

# 风险等级 → 徽章色（green / yellow / red）
RISK_COLORS: dict[str, str] = {
    "green": SUCCESS,
    "yellow": WARNING,
    "red": DANGER,
}

# 风险等级 → 中文徽章文案
RISK_LABELS: dict[str, str] = {
    "green": "低风险",
    "yellow": "中风险",
    "red": "高风险",
}

# 风险等级 → 整行背景色（任务表格用，正常保持白色）
RISK_ROW_BACKGROUNDS: dict[str, str] = {
    "低风险": SURFACE,
    "中风险": "#FFFBEB",   # 浅黄
    "高风险": "#FEF2F2",   # 浅红
}

# 提醒等级 → 展示色（键与 knowledge/rules/reminder.json 一致）
LEVEL_COLORS: dict[str, str] = {
    "normal": SUCCESS,
    "due_soon": WARNING,
    "urgent": URGENT,
    "overdue": DANGER,
}

# 任务状态 → 展示色
STATUS_COLORS: dict[str, str] = {
    "pending": TEXT_SUB,
    "confirmed": PRIMARY,
    "processing": PRIMARY,
    "urgent": WARNING,
    "completed": SUCCESS,
    "overdue": DANGER,
}


# ============================================================
# 二、全局 CSS
# ============================================================

APP_CSS: str = f"""
<style>
/* ---------- 全局 ---------- */
.stApp {{
    background: {BACKGROUND};
}}
html, body, [class*="st-"] {{
    font-family: "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Source Han Sans SC", sans-serif;
}}
.block-container {{
    padding-top: 2rem;
    padding-bottom: 2.4rem;
    max-width: 1240px;
}}
h1, h2, h3, h4 {{
    color: {TEXT_MAIN};
    letter-spacing: 0.2px;
}}

/* 一级标题：32px 加粗 + 左侧蓝色竖线 */
h1 {{
    font-size: 32px !important;
    font-weight: 700 !important;
    line-height: 1.3 !important;
    border-left: 4px solid {PRIMARY};
    padding-left: 14px;
    margin: 0 0 6px 0 !important;
}}
h2 {{ font-size: 20px !important; }}
h3 {{ font-size: 17px !important; }}

/* 副标题：13px 灰色 */
.cf-subtitle {{
    color: {TEXT_SUB};
    font-size: 13px;
    margin: 0 0 18px 18px;
}}

/* 右上角状态标签 */
.cf-tag {{
    display: inline-block;
    background: #ECFDF5;
    color: #15803D;
    border: 1px solid #BBF7D0;
    border-radius: {RADIUS};
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    margin-top: 14px;
}}

/* ---------- 卡片 ---------- */
.cf-card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {CARD_RADIUS};
    padding: 18px 20px;
    box-shadow: {SHADOW};
}}

/* ---------- KPI 卡片 ---------- */
.cf-kpi {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-top: 3px solid {PRIMARY};
    border-radius: {CARD_RADIUS};
    padding: 26px 26px;
    box-shadow: {SHADOW};
    height: 100%;
}}
.cf-kpi-label {{
    color: {TEXT_SUB};
    font-size: 13px;
    margin-bottom: 12px;
}}
.cf-kpi-value {{
    color: {TEXT_MAIN};
    font-size: 36px;
    font-weight: 700;
    line-height: 1.15;
}}
.cf-kpi-desc {{
    color: {TEXT_MUTED};
    font-size: 12px;
    margin-top: 14px;
}}
.cf-kpi-value-text {{
    font-size: 18px !important;
    font-weight: 700;
    line-height: 1.4;
}}

/* ---------- 今日重点督办横幅 ---------- */
.cf-banner {{
    background: linear-gradient(90deg, #EFF6FF 0%, #FFFFFF 100%);
    border: 1px solid #BFDBFE;
    border-left: 4px solid {PRIMARY};
    border-radius: {CARD_RADIUS};
    padding: 24px 26px;
    box-shadow: {SHADOW};
}}
.cf-banner-title {{
    color: #1E3A8A;
    font-size: 15px;
    font-weight: 700;
    margin-bottom: 12px;
}}
.cf-banner-text {{
    color: #334155;
    font-size: 14px;
    line-height: 1.9;
}}

/* ---------- 三步流程条 ---------- */
.cf-steps {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    margin: 4px 0 18px 0;
}}
.cf-step {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px;
    border-radius: {RADIUS};
    font-size: 13px;
    font-weight: 600;
    background: {SURFACE};
    border: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
.cf-step-num {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: #E2E8F0;
    color: {SURFACE};
    font-size: 12px;
}}
.cf-step-active {{
    background: {PRIMARY};
    border-color: {PRIMARY};
    color: {SURFACE};
}}
.cf-step-active .cf-step-num {{
    background: rgba(255, 255, 255, 0.3);
}}
.cf-step-done {{
    background: #EFF6FF;
    border-color: #BFDBFE;
    color: #1D4ED8;
}}
.cf-step-done .cf-step-num {{
    background: {PRIMARY};
}}
.cf-step-arrow {{
    color: #CBD5E1;
    font-size: 13px;
}}

/* ---------- 区块标题 ---------- */

/* ---------- AI 主动发现：状态标签与事项行 ---------- */
/* 小型圆角标签：浅底 + 深字，克制，不做成大色块 */
.cf-tag {{
    display: inline-block;
    padding: 2px 8px;
    font-size: 12px;
    line-height: 18px;
    border-radius: 4px;
    font-weight: 500;
    white-space: nowrap;
}}
.cf-tag--danger {{ background: #FEF2F2; color: {DANGER}; }}
.cf-tag--warning {{ background: #FFFBEB; color: #B45309; }}
.cf-tag--info {{ background: #EFF6FF; color: {PRIMARY}; }}
.cf-tag--muted {{ background: #F1F5F9; color: {TEXT_SUB}; }}

.cf-find {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-left: 3px solid {PRIMARY};
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 10px;
}}
.cf-find-head {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
}}
.cf-find-title {{
    color: {TEXT_MAIN};
    font-size: 15px;
    font-weight: 600;
}}
.cf-find-row {{
    font-size: 13px;
    line-height: 1.8;
    color: {TEXT_SUB};
}}
.cf-find-key {{ color: {TEXT_MUTED}; margin-right: 8px; }}
.cf-find-val {{ color: {TEXT_MAIN}; }}
.cf-find-advice {{ color: {PRIMARY}; }}
.cf-section {{
    display: flex;
    align-items: center;
    gap: 12px;
    border-left: 4px solid {PRIMARY};
    padding-left: 14px;
    margin: 30px 0 20px 0;
}}
.cf-section-title {{
    color: {TEXT_MAIN};
    font-size: 17px;
    font-weight: 700;
}}
.cf-section-caption {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}

/* ---------- 徽章 ---------- */
.cf-badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: {RADIUS};
    font-size: 12px;
    font-weight: 600;
    color: #ffffff;
    white-space: nowrap;
}}
.cf-badge-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 2px 0 4px 0;
}}
.cf-badge-item {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    padding: 6px 12px;
    font-size: 13px;
    color: {TEXT_MAIN};
}}

/* ---------- 统计行（卡片内纵向数值） ---------- */
.cf-level-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 11px 0;
    border-bottom: 1px dashed {BORDER};
    font-size: 14px;
    color: {TEXT_MAIN};
}}
.cf-level-row:last-child {{ border-bottom: none; }}
.cf-level-name {{ color: {TEXT_SUB}; }}
.cf-level-value {{ font-weight: 700; font-size: 18px; }}

/* ---------- 圆点统计行（风险列表） ---------- */
.cf-dot-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 0;
    border-bottom: 1px dashed {BORDER};
    font-size: 14px;
}}
.cf-dot-row:last-child {{ border-bottom: none; }}
.cf-dot-label {{
    display: flex;
    align-items: center;
    color: #334155;
}}
.cf-dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 8px;
}}
.cf-dot-value {{
    font-weight: 700;
    font-size: 16px;
    color: {TEXT_MAIN};
}}

/* ---------- 领导摘要 ---------- */
.cf-leader {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-left: 4px solid {PRIMARY};
    border-radius: {CARD_RADIUS};
    padding: 20px 22px;
    box-shadow: {SHADOW};
    margin-bottom: 18px;
}}
.cf-leader-title {{
    font-size: 16px;
    font-weight: 700;
    color: {TEXT_MAIN};
    margin-bottom: 10px;
}}
.cf-leader-text {{
    font-size: 14px;
    color: #334155;
    line-height: 1.95;
}}
.cf-leader-rule {{
    border: none;
    border-top: 1px solid {BORDER};
    margin: 16px 0 0 0;
}}

/* ---------- 提示条 ---------- */
.cf-hint {{
    background: #EFF6FF;
    border: 1px solid #BFDBFE;
    border-radius: {CARD_RADIUS};
    padding: 14px 18px;
    color: #1E3A8A;
    font-size: 13px;
    line-height: 1.9;
}}
.cf-note {{
    color: {TEXT_MUTED};
    font-size: 12px;
    margin-top: 18px;
}}

/* ---------- 列表 ---------- */
.cf-list {{
    margin: 0;
    padding: 0;
    list-style: none;
}}
.cf-list li {{
    position: relative;
    padding: 10px 0 10px 22px;
    border-bottom: 1px dashed {BORDER};
    color: {TEXT_MAIN};
    font-size: 14px;
    line-height: 1.7;
}}
.cf-list li:last-child {{ border-bottom: none; }}
.cf-list li::before {{
    content: "";
    position: absolute;
    left: 4px;
    top: 18px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: {PRIMARY};
}}

/* ---------- 空状态 ---------- */
.cf-empty {{
    background: {SURFACE};
    border: 1px dashed #CBD5E1;
    border-radius: {CARD_RADIUS};
    padding: 36px 24px;
    text-align: center;
    margin-bottom: 14px;
}}
.cf-empty-title {{
    color: {TEXT_MAIN};
    font-size: 17px;
    font-weight: 700;
    margin-bottom: 8px;
}}
.cf-empty-hint {{
    color: {TEXT_SUB};
    font-size: 14px;
}}

/* ---------- 文件信息卡 ---------- */
.cf-file-card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-left: 4px solid {SUCCESS};
    border-radius: {CARD_RADIUS};
    padding: 18px 20px;
    box-shadow: {SHADOW};
    margin: 6px 0 14px 0;
}}
.cf-file-head {{
    display: flex;
    align-items: center;
    gap: 8px;
    color: {SUCCESS};
    font-size: 14px;
    font-weight: 700;
    margin-bottom: 10px;
}}
.cf-file-dot {{
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: {SUCCESS};
}}
.cf-file-row {{
    display: flex;
    gap: 10px;
    font-size: 13px;
    color: {TEXT_MAIN};
    padding: 4px 0;
}}
.cf-file-label {{
    color: {TEXT_SUB};
    min-width: 76px;
}}

/* ---------- 侧边栏 ---------- */
section[data-testid="stSidebar"] {{
    background: {SURFACE};
    border-right: 1px solid {BORDER};
}}
section[data-testid="stSidebar"] > div:first-child {{
    padding-top: 1.4rem;
    padding-bottom: 1.2rem;
}}
section[data-testid="stSidebar"] .cf-brand-title {{
    color: {PRIMARY};
    font-size: 19px;
    font-weight: 700;
    margin-bottom: 2px;
}}
section[data-testid="stSidebar"] .cf-brand-sub {{
    color: {TEXT_SUB};
    font-size: 12px;
    margin-bottom: 10px;
}}
/* 导航项：隐藏单选圆点，做成菜单条目（压缩留白） */
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"] {{
    padding: 7px 12px;
    border-radius: 8px;
    margin-bottom: 0;
    transition: background 0.15s ease;
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"]:hover {{
    background: #EFF6FF;
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"] > div:first-child {{
    display: none;
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"] p {{
    color: {TEXT_MAIN};
    font-size: 14px;
    font-weight: 600;
}}

/* 侧边栏底部信息卡（灰白，不用深蓝铺满） */
section[data-testid="stSidebar"] .cf-side-card {{
    background: {BACKGROUND};
    border: 1px solid {BORDER};
    border-radius: {CARD_RADIUS};
    padding: 12px 14px;
    margin-top: 10px;
}}
section[data-testid="stSidebar"] .cf-side-card-title {{
    color: {TEXT_MAIN};
    font-size: 13px;
    font-weight: 700;
    margin-bottom: 2px;
}}
section[data-testid="stSidebar"] .cf-side-card-note {{
    color: {TEXT_MUTED};
    font-size: 12px;
    line-height: 1.7;
    margin-bottom: 8px;
}}
section[data-testid="stSidebar"] .cf-side-row {{
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 12px;
    color: {TEXT_SUB};
    padding: 2px 0;
}}
section[data-testid="stSidebar"] .cf-side-row span:last-child {{
    color: {TEXT_MAIN};
    font-weight: 600;
}}

/* ---------- 按钮：主按钮蓝底白字，次按钮白底蓝边 ---------- */
.stButton > button,
.stDownloadButton > button,
.stFormSubmitButton > button {{
    border-radius: {RADIUS};
    font-weight: 600;
    border: 1px solid {PRIMARY};
}}
.stButton > button[kind="primary"],
.stDownloadButton > button[kind="primary"],
.stFormSubmitButton > button[kind="primary"] {{
    background: {PRIMARY};
    color: #ffffff;
    border-color: {PRIMARY};
}}
.stButton > button[kind="primary"]:hover,
.stDownloadButton > button[kind="primary"]:hover,
.stFormSubmitButton > button[kind="primary"]:hover {{
    background: #1D4ED8;
    border-color: #1D4ED8;
    color: #ffffff;
}}
.stButton > button[kind="secondary"]:hover,
.stDownloadButton > button[kind="secondary"]:hover {{
    background: #EFF6FF;
    color: {PRIMARY};
}}

/* ---------- 上传组件中文化 ---------- */
section[data-testid="stFileUploaderDropzone"] {{
    background: {SURFACE};
    border: 1px dashed #CBD5E1;
    border-radius: {CARD_RADIUS};
    padding: 20px;
}}
section[data-testid="stFileUploaderDropzone"] div[data-testid="stFileUploaderDropzoneInstructions"] > div:last-child {{
    display: none;
}}
section[data-testid="stFileUploaderDropzone"] div[data-testid="stFileUploaderDropzoneInstructions"]::after {{
    content: "将文件拖拽到此处";
    display: block;
    color: {TEXT_MAIN};
    font-size: 15px;
    font-weight: 600;
    margin-top: 4px;
}}
section[data-testid="stFileUploaderDropzone"] button {{
    font-size: 0;
}}
section[data-testid="stFileUploaderDropzone"] button::after {{
    content: "选择文件";
    font-size: 14px;
    font-weight: 600;
    color: {PRIMARY};
}}

/* ---------- 表格与表单 ---------- */
div[data-testid="stDataFrame"] {{
    border-radius: {CARD_RADIUS};
    overflow: hidden;
    border: 1px solid {BORDER};
}}
div[data-testid="stForm"] {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {CARD_RADIUS};
    padding: 18px 20px;
    box-shadow: {SHADOW};
}}
</style>
"""


def inject() -> None:
    """注入全局样式（每次 rerun 执行一次，开销可忽略）。"""
    st.markdown(APP_CSS, unsafe_allow_html=True)


# ============================================================
# 三、小工具
# ============================================================


def badge_html(text: str, color: str) -> str:
    """生成彩色徽章的 HTML 片段。"""
    return f'<span class="cf-badge" style="background:{color}">{text}</span>'


def risk_badge_html(risk: str | None) -> str:
    """风险等级 → 中文徽章（正常 / 需确认 / 高风险）。"""
    key = (risk or "").strip()
    label = RISK_LABELS.get(key, "未评估")
    color = RISK_COLORS.get(key, TEXT_MUTED)
    return badge_html(label, color)


# 状态条样式标记：随 st.status 一起注入，使执行进度条与页面主题一致。
# 仅调整底色与边框，不隐藏任何文字或动画，保证用户能看到解析进度。
STATUS_MARKER: str = (
    "<style>"
    '[data-testid="stStatusWidget"] {'
    "background: transparent; border: none; box-shadow: none;"
    "}"
    '[data-testid="stExpander"] details {'
    "background: transparent;"
    "}"
    "</style>"
)

# 状态标签的语义配色（沿用既有语义色，不新建色系）
TAG_TONES: dict[str, str] = {
    "danger": "danger",
    "warning": "warning",
    "info": "info",
    "muted": "muted",
}


def tag_html(text: str, tone: str = "muted") -> str:
    """小型状态标签：浅色背景 + 深色文字，克制，不做成大色块。"""
    return f'<span class="cf-tag cf-tag--{TAG_TONES.get(tone, "muted")}">{text}</span>'


def finding_html(tag_html_: str, title: str, situation: str, advice: str) -> str:
    """AI 主动发现的事项行：状态标签 + 标题 / 当前情况 / AI 建议。"""
    return (
        f'<div class="cf-find">'
        f'<div class="cf-find-head">{tag_html_}<span class="cf-find-title">{title}</span></div>'
        f'<div class="cf-find-row"><span class="cf-find-key">当前情况</span>'
        f'<span class="cf-find-val">{situation}</span></div>'
        f'<div class="cf-find-row"><span class="cf-find-key">处理建议</span>'
        f'<span class="cf-find-advice">{advice}</span></div>'
        f"</div>"
    )
