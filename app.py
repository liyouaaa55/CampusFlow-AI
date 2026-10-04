"""CampusFlow AI —— Streamlit 应用入口（V2.0 产品化界面）。

职责：只做「页面配置 + 导航 + 分发」，所有业务逻辑都在 agent / tools / database 层：
- 智能驾驶舱      ReportAgent 驾驶舱指标与今日督办建议
- 会议材料上传    parser_tool → TaskAgent → PlannerAgent（结果暂存，不入库）
- 事项智能识别    人工确认后 sqlite_db.batch_create() 入库
- 任务督办中心    list_tasks / update_status / get_logs
- 执行情况汇报    ReportAgent 周报与导出

运行方式：streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from config import settings
from ui.components import cards, state, styles
from ui.pages import (
    dashboard_page,
    report_page,
    review_page,
    tasks_page,
    upload_page,
)

# 产品定位文案（仅界面展示，不改动 config/settings.py）
APP_SUBTITLE: str = "面向高校基层事务的 AI 闭环督办平台"

# 五个页面的名称与顺序（侧边栏流程化导航）
PAGES: tuple[str, ...] = (
    "智能驾驶舱",
    "会议材料上传",
    "事项智能识别",
    "任务督办中心",
    "执行情况汇报",
)

# 页面 → 渲染函数
PAGE_RENDERERS: dict[str, object] = {
    "智能驾驶舱": dashboard_page.render,
    "会议材料上传": upload_page.render,
    "事项智能识别": review_page.render,
    "任务督办中心": tasks_page.render,
    "执行情况汇报": report_page.render,
}


def render_sidebar() -> str:
    """渲染侧边栏导航，返回当前选中的页面名称。

    导航控件绑定 session_state[KEY_NAV]，因此页面内可通过 state.goto() 跳转。
    """
    with st.sidebar:
        st.markdown(f'<div class="cf-brand-title">{settings.APP_NAME}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="cf-brand-sub">{APP_SUBTITLE}</div>', unsafe_allow_html=True)

        # 必须在 radio 实例化之前处理导航键：
        # 1) 纠正非法/缺失的取值；2) 应用页面内发起的跳转请求。
        state.ensure_nav_valid(PAGES)
        state.apply_nav_request(PAGES)

        page = st.radio("功能导航", PAGES, key=state.KEY_NAV, label_visibility="collapsed")

        # 底部信息卡（灰白卡片，不整块深蓝）
        cards.side_card(
            title="演示数据",
            note="全部数据为演示数据（Synthetic Data），仅用于展示。",
            rows=[
                ("任务数据库", settings.DB_PATH.name),
                ("当前版本", f"v{settings.APP_VERSION}"),
            ],
        )

        # 启动自检：只提示，不阻断（一切正常时不渲染任何内容）
        _render_startup_notice()

    return str(page)


def _render_startup_notice() -> None:
    """启动自检提示：数据目录 / 知识库目录 / 大模型配置。

    刻意做成「有问题才提示」：全部正常时页面上不会多出任何元素。
    提示仅用于告知，不阻断应用启动，也不改变五个页面的内容。
    """
    warnings = settings.environment_warnings()
    if not warnings:
        return

    st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)
    for message in warnings:
        st.warning(message)


def main() -> None:
    """应用主入口：配置页面、注入样式、分发到对应页面。"""
    st.set_page_config(
        page_title=settings.APP_NAME,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    styles.inject()

    page = render_sidebar()

    # 分发到具体页面（页面模块只暴露 render()，并自行渲染标题）
    renderer = PAGE_RENDERERS[page]
    renderer()  # type: ignore[operator]


if __name__ == "__main__":
    main()
