"""styles 模块常量完整性守卫（回归防线）。

背景
----
P0 事故：`ui/components/styles.py` 曾因误用 git checkout 丢失过未提交改动，
其中包括只在「点击上传解析」时才用到的 `STATUS_MARKER`。该缺失导致上传页在
真实点击时抛 `AttributeError: module 'ui.components.styles' has no attribute ...`，
而当时的 pytest 与页面加载检查**全部通过**——因为没有任何测试真正触发那个按钮。

本文件把「所有被引用的 styles 常量必须存在」变成显式约束，
使这类错误在测试阶段即暴露，而不是留到演示现场。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ui.components import styles

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
_PATTERN = re.compile(r"styles\.([A-Z_][A-Z0-9_]*)")


def _referenced_names() -> set[str]:
    """扫描 ui/ 下所有模块，收集被引用的 styles 常量名。"""
    names: set[str] = set()
    for path in UI_DIR.rglob("*.py"):
        if path.name == "styles.py":
            continue
        names.update(_PATTERN.findall(path.read_text(encoding="utf-8")))
    return names


def test_referenced_constants_all_exist() -> None:
    """所有被引用的 styles 常量必须真实存在。"""
    missing = sorted(n for n in _referenced_names() if not hasattr(styles, n))
    assert not missing, f"ui 模块引用了不存在的 styles 常量：{missing}"


def test_status_marker_exists() -> None:
    """STATUS_MARKER 被上传页与驾驶舱演示流程使用，必须存在且为样式串。"""
    assert hasattr(styles, "STATUS_MARKER")
    marker = styles.STATUS_MARKER
    assert isinstance(marker, str)
    assert "<style" in marker
    # 不得隐藏文字或动画，否则用户看不到解析进度
    for forbidden in ("display:none", "visibility:hidden", "opacity:0"):
        assert forbidden not in marker.replace(" ", ""), f"STATUS_MARKER 不得包含 {forbidden}"


def test_status_icon_css_hides_only_decoration() -> None:
    """STATUS_ICON_CSS 只允许隐藏装饰图标，不得影响状态文字与解析正文。

    背景：Streamlit 1.37 未加载 Material Icons 字体，st.status 标题左侧的
    stExpanderIconCheck 会退化为英文单词 "check" 显示在界面上（实测确认）。
    该规则由上传页与驾驶舱演示流程注入，必须保持作用域受限。
    """
    assert hasattr(styles, "STATUS_ICON_CSS"), "上传页与驾驶舱引用了 STATUS_ICON_CSS"
    css = styles.STATUS_ICON_CSS
    assert isinstance(css, str)
    assert "<style" in css, "必须是可独立注入的完整 style 标签"
    # 只允许命中 st.status 状态图标，不得波及普通 st.expander 的展开箭头
    assert "stExpanderIconCheck" in css, "必须命中 st.status 状态图标 testid"
    assert "stExpanderIconChevron" not in css, "不得影响普通 st.expander 箭头"
    # 只隐藏图标本身，不得波及状态文字（解析完成）与 ①②③ 解析正文
    assert "stMarkdownContainer" not in css, "不得作用于状态文字"
    assert "display: none" in css
    assert "!important" in css, "需压过 Streamlit 内联样式，否则可能被覆盖"
    # 历史踩坑：锚点用不存在的类名导致规则静默失效，改为断言只用 testid 锚点
    assert ":has(" not in css, "不依赖 :has()，其浏览器支持性不稳定"
    assert "cf-status-marker" not in css, "该类名从未被注入，不可作为锚点"


def test_known_constants_present() -> None:
    """关键常量逐个断言，避免逐个被删时才被发现。"""
    for name in (
        "STATUS_MARKER",
        "PRIMARY",
        "DANGER",
        "WARNING",
        "SUCCESS",
        "SURFACE",
        "BORDER",
        "TEXT_MAIN",
        "TEXT_SUB",
        "TEXT_MUTED",
        "RISK_LABELS",
        "RISK_ROW_BACKGROUNDS",
        "RISK_COLORS",
        "STATUS_COLORS",
        "LEVEL_COLORS",
    ):
        assert hasattr(styles, name), f"缺少常量 {name}"


def test_public_helpers_exist() -> None:
    """UI 层调用的样式辅助函数必须存在。"""
    for name in ("inject", "badge_html", "risk_badge_html", "tag_html", "finding_html"):
        assert hasattr(styles, name), f"缺少函数 {name}"


def test_labels_module_constants_used_by_pages_exist() -> None:
    """同样校验 ui.labels：页面引用的中文映射常量不得缺失。"""
    from ui import labels

    pattern = re.compile(r"labels\.([A-Z_][A-Z0-9_]*)")
    missing: set[str] = set()
    for path in UI_DIR.rglob("*.py"):
        if path.name == "labels.py":
            continue
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            if not hasattr(labels, name):
                missing.add(name)
    assert not missing, f"ui 模块引用了不存在的 labels 常量：{sorted(missing)}"