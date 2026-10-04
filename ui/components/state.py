"""session_state 封装：承载「上传 → 识别 → 人工确认」的暂存数据。

约定：本模块只做状态读写，不做任何业务计算；入库动作由页面调用
database.sqlite_db.batch_create() 完成。
"""

from __future__ import annotations

from typing import Any, Sequence

import streamlit as st

# ---------- session_state 键 ----------
KEY_PARSED_TASKS: str = "cf_parsed_tasks"       # PlannerAgent 输出的待确认任务（list[dict]）
KEY_PARSED_FILE: str = "cf_parsed_file"         # 来源文件名
KEY_PARSED_TEXT: str = "cf_parsed_text"         # 解析出的纯文本（用于预览）
KEY_FLASH: str = "cf_flash_message"             # 跨 rerun 的一次性提示
KEY_NAV: str = "cf_nav_page"                    # 当前页面（侧边栏导航控件自身的 key）
KEY_NAV_REQUEST: str = "cf_nav_request"         # 程序化跳转请求（普通键，非控件键）


# ---------- 暂存识别结果 ----------


def save_parsed(tasks: Sequence[dict[str, Any]], filename: str, text: str) -> None:
    """保存识别结果（尚未入库）。"""
    st.session_state[KEY_PARSED_TASKS] = list(tasks)
    st.session_state[KEY_PARSED_FILE] = filename
    st.session_state[KEY_PARSED_TEXT] = text


def get_parsed_tasks() -> list[dict[str, Any]]:
    """读取待确认任务列表（默认空列表）。"""
    return list(st.session_state.get(KEY_PARSED_TASKS) or [])


def get_parsed_file() -> str:
    """读取来源文件名。"""
    return str(st.session_state.get(KEY_PARSED_FILE) or "")


def get_parsed_text() -> str:
    """读取解析出的纯文本。"""
    return str(st.session_state.get(KEY_PARSED_TEXT) or "")


def has_parsed() -> bool:
    """是否存在待确认的识别结果。"""
    return bool(get_parsed_tasks())


def clear_parsed() -> None:
    """清空暂存的识别结果。"""
    for key in (KEY_PARSED_TASKS, KEY_PARSED_FILE, KEY_PARSED_TEXT):
        st.session_state.pop(key, None)


# ---------- 一次性提示（跨 rerun 显示） ----------


def set_flash(message: str) -> None:
    """记录一条提示，供下一次 rerun 展示。"""
    st.session_state[KEY_FLASH] = message


def pop_flash() -> str | None:
    """取出并清除提示。"""
    return st.session_state.pop(KEY_FLASH, None)


# ---------- 页面跳转（仅编排导航，不涉及业务） ----------


def goto(page: str) -> None:
    """请求切换到指定页面并立即重跑。

    注意：不能直接写 KEY_NAV。导航控件（radio）在本轮已经实例化，
    Streamlit 禁止事后修改其绑定键（否则抛 StreamlitAPIException）。
    因此这里只写入独立的请求键，由 apply_nav_request() 在下一轮
    控件实例化之前应用。
    """
    st.session_state[KEY_NAV_REQUEST] = page
    st.rerun()


def apply_nav_request(valid_pages: Sequence[str] | None = None) -> None:
    """应用跳转请求：必须在导航控件实例化**之前**调用。

    valid_pages 用于校验目标页面是否合法，非法请求直接丢弃。
    """
    requested = st.session_state.pop(KEY_NAV_REQUEST, None)
    if not requested:
        return
    if valid_pages is not None and requested not in valid_pages:
        return
    st.session_state[KEY_NAV] = requested


def ensure_nav_valid(valid_pages: Sequence[str]) -> None:
    """兜底：保证导航键的取值始终在合法页面范围内。

    导航键既可能在首次运行时不存在，也可能因外部写入而失效，
    这两种情况都会让 radio 抛异常，故在实例化之前统一纠正。
    """
    if not valid_pages:
        return
    if st.session_state.get(KEY_NAV) not in valid_pages:
        st.session_state[KEY_NAV] = valid_pages[0]


def current_page() -> str | None:
    """读取当前选中的页面名称（未初始化时为 None）。"""
    value = st.session_state.get(KEY_NAV)
    return str(value) if value else None
