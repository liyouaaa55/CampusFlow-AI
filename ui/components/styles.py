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
    padding-left: 2.4rem;
    padding-right: 2.4rem;
    max-width: 1240px;
    /* 窗口比 1240px 宽时内容居中，两侧留白均匀 */
    margin: 0 auto;
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
    border-radius: 8px !important;
    padding: 14px 18px;
    max-width: 100%;
    box-sizing: border-box;
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
/* 在卡片内自适应宽度（覆盖 Streamlit 内联的固定像素宽）：左为拖拽提示
   文字、右为「选择文件」按钮，使按钮右边缘与下方示例下拉箭头右边缘
   严格对齐（二者同处卡片内容右）；窄屏自动换行避免横向溢出。
   注：DOM 编辑器手填的 width:40203px 属误拖，会撑爆页面，故不采用。 */
section[data-testid="stFileUploaderDropzone"] {{
    display: flex !important;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
    box-sizing: border-box !important;
    width: 100% !important;
    max-width: 100% !important;
    margin: 0 !important;
    padding: 16px 20px !important;
    background: {SURFACE};
    border: 1px dashed #CBD5E1;
    border-radius: 8px !important;
}}
section[data-testid="stFileUploaderDropzone"] div[data-testid="stFileUploaderDropzoneInstructions"] > div:last-child {{
    display: none;
}}
/* 强制清理拖拽框内所有直接子元素（图标 / 文字 / 按钮）的原生多余外边距 */
section[data-testid="stFileUploaderDropzone"] > * {{
    margin: 0 !important;
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
    flex-shrink: 0 !important;
    /* 推到最右并外移抵消 dropzone 右内边距 20px，使按钮右边缘贴拖拽框
       右边框；与示例下拉框箭头（同处卡片内容右）右边缘严格对齐。 */
    margin-left: auto !important;
    margin-right: -20px !important;
}}
section[data-testid="stFileUploaderDropzone"] button::after {{
    content: "选择文件";
    font-size: 13px;
    font-weight: 500;
    color: {TEXT_SUB};
}}

/* 示例下拉框：容器与控件均用 !important 强制 margin:0、width:100%、
   max-width:100%，覆盖原生多余内边距，与上传拖拽框外框左右对齐、盒子
   等宽；圆角与拖拽框、底部提示栏统一 8px。下拉箭头贴控件右边框，其
   右边缘与「选择文件」按钮右边缘同处卡片内容右，二者严格对齐。 */
.stSelectbox {{
    width: 100% !important;
    max-width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
    box-sizing: border-box !important;
}}
div[data-baseweb="select"] {{
    width: 100% !important;
    max-width: 100% !important;
}}
div[data-baseweb="select"] > div {{
    border-radius: 8px !important;
    width: 100% !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
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

/* ---------- 上传 / 示例 / 结果 卡片容器 ----------
   由 ui/pages/upload_page.py 用 st.container() 配合内部隐藏标记
   (data-cf-card = upload|sample|pending) 定位，承载白色卡片外框，
   统一页面视觉层级。Streamlit 1.37 的 st.container 不支持 key 参数，
   故改用 :has() 锚定内部标记的方式（现代浏览器均支持）。
   并用 :not() 排除 .block-container 的直接子块，避免整页主体被框住。 */
[data-testid="stVerticalBlock"]:has([data-cf-card]):not(.block-container > [data-testid="stVerticalBlock"]) {{
    box-sizing: border-box !important;
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {CARD_RADIUS};
    padding: 22px 26px;
    box-shadow: {SHADOW};
    margin: 6px 0 18px 0;
}}
/* 卡片内首部区块标题上边距收窄，避免卡片内再留大空白 */
[data-testid="stVerticalBlock"]:has([data-cf-card]):not(.block-container > [data-testid="stVerticalBlock"]) .cf-section {{
    margin-top: 0;
}}

/* ---------- 主操作按钮放大（品牌蓝填充沿用 primary） ---------- */
.stButton > button[kind="primary"],
.stFormSubmitButton > button[kind="primary"] {{
    font-size: 15px !important;
    padding: 0.55rem 1.6rem !important;
    font-weight: 700 !important;
}}

/* ---------- 解析进度三步：序号加粗、标题与内容分层 ---------- */
/* 顶部留 10px，与「解析完成」状态标题保持间距、不紧贴；底部收紧。 */
.cf-parse-steps {{ margin: 10px 0 2px 0; }}
.cf-parse-step {{
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 7px 0;
    font-size: 14px;
    line-height: 1.6;
    border-bottom: 1px dashed #EDF2F7;
}}
.cf-parse-step:last-child {{ border-bottom: none; }}
.cf-parse-idx {{ font-weight: 700; color: {PRIMARY}; font-size: 15px; flex: 0 0 auto; }}
.cf-parse-title {{ font-weight: 600; color: {TEXT_MAIN}; flex: 0 0 auto; white-space: nowrap; }}
/* 内容列可换行收缩（长文件名不撑爆布局），min-width:0 是 flex 子换行前提 */
.cf-parse-content {{ color: {TEXT_SUB}; overflow-wrap: anywhere; min-width: 0; }}

/* st.status 内容区默认有 padding-top，使 ①②③ 整体下沉、显得空。
   去除后三步上移紧凑；与「解析完成」的间距由 .cf-parse-steps 的 margin-top 提供。 */
[data-testid="stStatusWidget"] details > div {{
    padding-top: 0 !important;
    padding-bottom: 0 !important;
}}

/* ---------- 底部提示栏与上传/示例卡片宽度对齐 ----------
   上传框、示例下拉都在卡片内容区内（左右各内缩 26px），而底部
   提示栏裸露在主容器满宽，导致三者宽度不一致。footer 容器内的
   提示条与说明文字左右内缩 26px，与卡片内容区精确对齐；宽度均
   为相对值，窗口缩放自动适配，不产生横向滚动条。 */
[data-testid="stVerticalBlock"]:has([data-cf-align="footer"]):not(.block-container > [data-testid="stVerticalBlock"]) .cf-hint {{
    padding: 14px 20px;
    margin-left: 26px;
    margin-right: 26px;
}}
[data-testid="stVerticalBlock"]:has([data-cf-align="footer"]):not(.block-container > [data-testid="stVerticalBlock"]) .cf-note {{
    margin-left: 26px;
    margin-right: 26px;
}}

/* ---------- 标题与步骤条与卡片内容区对齐 ----------
   标题 / 副标题 / 步骤条原来贴着主容器左（2.4rem），而卡片内容内缩 26px，
   导致左边界比卡片内容靠左。这里统一内缩到卡片内容区，使整页左边界一致；
   宽度均为相对值，窗口缩放自动适配、不横向溢出。
   h1 自带 padding-left:14px，故 margin-left:12px 合计 26px 与卡片内容对齐。 */
[data-testid="stVerticalBlock"]:has([data-cf-align="header"]):not(.block-container > [data-testid="stVerticalBlock"]) h1 {{
    margin-left: 12px;
}}
[data-testid="stVerticalBlock"]:has([data-cf-align="header"]):not(.block-container > [data-testid="stVerticalBlock"]) .cf-subtitle,
[data-testid="stVerticalBlock"]:has([data-cf-align="header"]):not(.block-container > [data-testid="stVerticalBlock"]) .cf-steps {{
    margin-left: 26px;
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
    # 进度条容器去除默认外边距，避免与下方「当前待确认结果」区间隔过大
    '[data-testid="stStatusWidget"] {'
    "background: transparent; border: none; box-shadow: none; margin: 0 !important;"
    "}"
    '[data-testid="stExpander"] details {'
    "background: transparent;"
    "}"
    "</style>"
)

# st.status 展开后标题左侧的 Material 图标（testid: stExpanderIconCheck）。
# Streamlit 1.37 未加载 Material Icons 字体，该图标会退化为英文单词
# "check" 直接显示在界面上（实测确认），因此在此隐藏。
#
# 作用域：stExpanderIconCheck 是 st.status 状态图标专属的 testid，普通
# st.expander 的展开箭头是 stExpanderIconChevron，两者不同，因此无需再用
# 祖先选择器限定作用域。
#
# 历史踩坑：曾写成 `details:has(.cf-status-marker) ...`，但项目从未注入过
# .cf-status-marker 这个类，整条规则匹配不到任何元素，修复静默失效、
# 页面上的 "check" 单词纹丝不动。教训：锚点必须用真实存在的 testid。
#
# 为何不并入 STATUS_MARKER：
#   tests/test_styles_contract.py::test_status_marker_exists 明确断言
#   STATUS_MARKER 不得包含 display:none（防止把解析正文与动画一起藏掉）。
#   本条只隐藏纯装饰图标，label 文字（解析完成 / 解析失败）与 ①②③ 正文
#   均不受影响，语义上本就是两件不同的事，故单独定义并单独注入。
STATUS_ICON_CSS: str = '<style>[data-testid="stExpanderIconCheck"] { display: none !important; }</style>'


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
