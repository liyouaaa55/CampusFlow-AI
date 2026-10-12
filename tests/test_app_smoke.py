"""入口冒烟测试：确认 Streamlit 应用能够无异常启动，且导航含 5 个页面。

使用 Streamlit 官方 AppTest 工具，无需真正启动 Web 服务。
"""

from __future__ import annotations

import pytest

from config import settings

# AppTest 需要 import streamlit，未安装时跳过该测试
pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402  （需在 importorskip 之后导入）


@pytest.fixture(scope="module")
def app() -> "AppTest":
    """加载并运行一次 app.py，模块内复用，避免重复启动开销。"""
    instance = AppTest.from_file(str(settings.BASE_DIR / "app.py"), default_timeout=60)
    instance.run()
    return instance


def test_app_runs_without_exception(app: "AppTest") -> None:
    """应用运行期间不应抛出任何异常。"""
    assert list(app.exception) == []


def test_app_has_five_pages(app: "AppTest") -> None:
    """侧边栏导航必须包含全部 5 个页面。

    v3.0 第一阶段：原「会议材料上传」与「事项智能识别」合并为
    「会议材料与任务生成」，页面数由 6 回到 5。
    断言强度不变（仍是与 ui/pages/nav.py 的 PAGES 逐项精确比对）。
    """
    from ui.pages import nav

    options = list(app.radio[0].options)
    assert options == list(nav.PAGES)


def test_homepage_renders_title(app: "AppTest") -> None:
    """首页应渲染一级标题「智能驾驶舱」。"""
    assert app.title[0].value == "智能驾驶舱"
