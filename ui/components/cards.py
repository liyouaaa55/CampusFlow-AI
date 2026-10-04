"""卡片类组件：页面标题、KPI 卡片、横幅、流程条、摘要卡、空状态、提示条、徽章行。

只做渲染，不访问数据库、不调用 Agent、不派生任何业务字段。
所有文案与数值均由调用方（页面）传入。
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping, Sequence

import streamlit as st

from ui.components import styles


# ============================================================
# 一、页面标题
# ============================================================


def page_header(title: str, subtitle: str | None = None, tag: str | None = None) -> None:
    """页面标题区：32px 主标题 + 13px 灰色副标题（右侧可挂状态标签）。

    使用 st.title 渲染，保持与 Streamlit 语义标题一致。
    """
    if not tag:
        st.title(title)
        if subtitle:
            st.markdown(f'<div class="cf-subtitle">{escape(subtitle)}</div>', unsafe_allow_html=True)
        return

    title_col, tag_col = st.columns([5, 2])
    with title_col:
        st.title(title)
        if subtitle:
            st.markdown(f'<div class="cf-subtitle">{escape(subtitle)}</div>', unsafe_allow_html=True)
    with tag_col:
        st.markdown(f'<div style="text-align:right"><span class="cf-tag">{escape(tag)}</span></div>', unsafe_allow_html=True)


def section(title: str, caption: str | None = None) -> None:
    """区块标题（带蓝色竖线），caption 为可选的灰色补充说明。"""
    caption_html = f'<span class="cf-section-caption">{escape(caption)}</span>' if caption else ""
    st.markdown(
        f'<div class="cf-section"><span class="cf-section-title">{escape(title)}</span>{caption_html}</div>',
        unsafe_allow_html=True,
    )


# ============================================================
# 二、指标卡片
# ============================================================


def kpi_row(items: Sequence[Mapping[str, Any]], columns: int | None = None) -> None:
    """一行卡片（KPI 或摘要卡）。

    每个 item 支持：
        label        卡片标题
        value        卡片数值（字符串或数字）
        desc         可选的补充说明
        color        顶部色条与数值颜色，默认主色
        text         True 时用小号字体展示文本型数值
    """
    column_count = columns or len(items)
    if column_count <= 0:
        return

    for column, item in zip(st.columns(column_count), items):
        with column:
            st.markdown(_kpi_html(item), unsafe_allow_html=True)


def _kpi_html(item: Mapping[str, Any]) -> str:
    """生成单个卡片的 HTML。"""
    color = str(item.get("color") or styles.PRIMARY)
    label = escape(str(item.get("label") or ""))
    value = escape(str(item.get("value")))
    desc = item.get("desc")
    desc_html = f'<div class="cf-kpi-desc">{escape(str(desc))}</div>' if desc else ""
    value_class = "cf-kpi-value cf-kpi-value-text" if item.get("text") else "cf-kpi-value"

    return (
        f'<div class="cf-kpi" style="border-top-color:{color}">'
        f'<div class="cf-kpi-label">{label}</div>'
        f'<div class="{value_class}" style="color:{color}">{value}</div>'
        f"{desc_html}"
        f"</div>"
    )


# ============================================================
# 三、横幅 / 流程条 / 摘要卡
# ============================================================


def banner(title: str, lines: Sequence[str]) -> None:
    """横幅卡片（今日重点督办），正文支持多行。"""
    body = "".join(f"<div>{escape(line)}</div>" for line in lines)
    st.markdown(
        f'<div class="cf-banner"><div class="cf-banner-title">{escape(title)}</div>'
        f'<div class="cf-banner-text">{body}</div></div>',
        unsafe_allow_html=True,
    )


def steps(items: Sequence[str], current: int) -> None:
    """三步流程条：current 之前的为已完成，current 高亮蓝色，其余灰色。"""
    if not items:
        return

    parts: list[str] = []
    for index, label in enumerate(items):
        if index < current:
            css = "cf-step cf-step-done"
        elif index == current:
            css = "cf-step cf-step-active"
        else:
            css = "cf-step"
        parts.append(
            f'<div class="{css}"><span class="cf-step-num">{index + 1}</span>'
            f"<span>{escape(str(label))}</span></div>"
        )
        if index < len(items) - 1:
            parts.append('<span class="cf-step-arrow">──▶</span>')

    st.markdown(f'<div class="cf-steps">{"".join(parts)}</div>', unsafe_allow_html=True)


def side_card(title: str, note: str, rows: Sequence[tuple[str, str]]) -> None:
    """侧边栏底部信息卡（灰白底，12~13px）。"""
    rows_html = "".join(
        f'<div class="cf-side-row"><span>{escape(str(label))}</span>'
        f"<span>{escape(str(value))}</span></div>"
        for label, value in rows
    )
    st.markdown(
        f'<div class="cf-side-card"><div class="cf-side-card-title">{escape(title)}</div>'
        f'<div class="cf-side-card-note">{escape(note)}</div>{rows_html}</div>',
        unsafe_allow_html=True,
    )


def leader_summary(lines: Sequence[str], title: str = "领导摘要") -> None:
    """领导摘要卡：结论式叙述 + 底部分隔线。"""
    body = "".join(f"<div>{escape(line)}</div>" for line in lines)
    st.markdown(
        f'<div class="cf-leader"><div class="cf-leader-title">{escape(title)}</div>'
        f'<div class="cf-leader-text">{body}</div><hr class="cf-leader-rule"></div>',
        unsafe_allow_html=True,
    )


# ============================================================
# 四、统计视图
# ============================================================


def level_stats(rows: Sequence[tuple[str, int, str]]) -> None:
    """卡片内的纵向统计（[(名称, 数量, 颜色), ...]）。"""
    if not rows:
        return

    body = "".join(
        f'<div class="cf-level-row"><span class="cf-level-name">{escape(str(name))}</span>'
        f'<span class="cf-level-value" style="color:{color}">{int(count)}</span></div>'
        for name, count, color in rows
    )
    st.markdown(f'<div class="cf-card">{body}</div>', unsafe_allow_html=True)


def dot_list(rows: Sequence[tuple[str, int, str]]) -> None:
    """圆点统计列表（[(名称, 数量, 颜色), ...]），用于风险事项概览。"""
    if not rows:
        return

    body = "".join(
        f'<div class="cf-dot-row"><span class="cf-dot-label">'
        f'<span class="cf-dot" style="background:{color}"></span>{escape(str(name))}</span>'
        f'<span class="cf-dot-value">{int(count)}</span></div>'
        for name, count, color in rows
    )
    st.markdown(f'<div class="cf-card">{body}</div>', unsafe_allow_html=True)


# ============================================================
# 五、提示与列表
# ============================================================


def hint(lines: Sequence[str]) -> None:
    """浅蓝色提示条（支持多行）。"""
    body = "<br>".join(escape(line) for line in lines)
    st.markdown(f'<div class="cf-hint">{body}</div>', unsafe_allow_html=True)


def note(text: str) -> None:
    """页面底部灰色说明文字。"""
    st.markdown(f'<div class="cf-note">{escape(text)}</div>', unsafe_allow_html=True)


def bullet_list(items: Sequence[str]) -> None:
    """蓝色圆点列表（用于督办建议等文本条目）。"""
    if not items:
        return
    rows = "".join(f"<li>{escape(str(item))}</li>" for item in items)
    st.markdown(f'<ul class="cf-list">{rows}</ul>', unsafe_allow_html=True)


def badge_row(items: Sequence[tuple[str, str, str]]) -> None:
    """徽章行：[(条目文案, 徽章文字, 徽章颜色), ...]。"""
    if not items:
        return

    chips = "".join(
        f'<span class="cf-badge-item">{escape(str(text))}'
        f'<span class="cf-badge" style="background:{color}">{escape(str(label))}</span></span>'
        for text, label, color in items
    )
    st.markdown(f'<div class="cf-badge-row">{chips}</div>', unsafe_allow_html=True)


# ============================================================
# 六、空状态与文件卡
# ============================================================


def empty_state(title: str, hint: str | None = None, action_label: str | None = None) -> bool:
    """空状态卡片；返回是否点击了动作按钮。"""
    hint_html = f'<div class="cf-empty-hint">{escape(hint)}</div>' if hint else ""
    st.markdown(
        f'<div class="cf-empty"><div class="cf-empty-title">{escape(title)}</div>{hint_html}</div>',
        unsafe_allow_html=True,
    )

    if not action_label:
        return False

    left, middle, right = st.columns([1, 1, 1])
    with middle:
        return bool(st.button(action_label, use_container_width=True))


def file_card(name: str, size_text: str, source_label: str, status_text: str) -> None:
    """上传成功后的文件信息卡（绿色左描边 + 成功图标）。"""
    rows = [
        ("文件名称", name),
        ("文件大小", size_text),
        ("来源类型", source_label),
        ("当前状态", status_text),
    ]
    rows_html = "".join(
        f'<div class="cf-file-row"><span class="cf-file-label">{escape(str(label))}</span>'
        f"<span>{escape(str(value))}</span></div>"
        for label, value in rows
    )
    st.markdown(
        f'<div class="cf-file-card">'
        f'<div class="cf-file-head"><span class="cf-file-dot"></span><span>已选择文件</span></div>'
        f"{rows_html}"
        f"</div>",
        unsafe_allow_html=True,
    )
