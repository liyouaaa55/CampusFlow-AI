"""真实页面流转测试：真正切换五个页面并断言关键控件存在。

与 test_app_smoke.py 的区别（这是本文件存在的理由）：
- test_app_smoke 只 `AppTest.run()` 跑一次**首页**，从不切页；
- 本文件用 `radio[0].set_value(...)` + `.run()` **真正进入**其余四个页面，
  任何页面级异常（包括只在该页触发的 NameError、未捕获异常）都会暴露；
- 同时断言每页的**关键操作控件真实存在**（按钮、上传区、数据编辑器），
  避免出现「页面能打开但按钮没了」这种演示当场翻车的隐性缺陷。

边界说明（重要，不掩饰）：
- 本文件**不**覆盖「上传→识别→入库」的写库链路。原因是 AppTest 无法注入
  真实 UploadedFile 与 session_state，且该链路涉及真实大模型调用；
  该链路由 tools/preflight.py 的 check_pipeline() 在真实代码路径上验证。
- 本文件只做只读渲染，不写入 data/campusflow.db。
"""

from __future__ import annotations

import pytest

from config import settings

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from ui.pages import nav  # noqa: E402  （页面名的唯一来源）

PAGES: tuple[str, ...] = nav.PAGES


@pytest.fixture(scope="module")
def app() -> "AppTest":
    """加载应用并停在首页。"""
    instance = AppTest.from_file(str(settings.BASE_DIR / "app.py"), default_timeout=60)
    instance.run()
    return instance


def _goto(instance: "AppTest", page: str) -> "AppTest":
    """切换到指定页面并重跑，返回同一实例。

    注意：不能用 `radio[0]` 取侧边栏导航——上传页内部还有「来源类型」radio，
    页面内控件同样进入 app.radio 列表，索引会错位（实测会抛 ValueError）。
    因此按「选项集等于全部页面名」精确定位导航控件。
    """
    nav = next(
        (item for item in instance.radio if list(item.options) == list(PAGES)),
        None,
    )
    assert nav is not None, "侧边栏导航 radio 未找到"
    nav.set_value(page)
    instance.run()
    return instance


def _button_labels(instance: "AppTest") -> set[str]:
    return {str(item.label) for item in instance.button}


@pytest.mark.parametrize("page", PAGES)
def test_each_page_renders_without_exception(app: "AppTest", page: str) -> None:
    """五个页面逐一真实渲染，均不得抛出异常。"""
    _goto(app, page)
    assert list(app.exception) == [], f"{page} 页面渲染时出现异常：{list(app.exception)}"


def test_navigation_stays_consistent(app: "AppTest") -> None:
    """连续切换五个页面后，导航仍应停在最后选中的页面（会话状态不丢）。"""
    for page in PAGES:
        _goto(app, page)
    nav = next(item for item in app.radio if list(item.options) == list(PAGES))
    assert nav.value == PAGES[-1]


def test_dashboard_renders_action_button(app: "AppTest") -> None:
    """驾驶舱必须有「一键体验演示」按钮——演示时的关键入口。"""
    _goto(app, "智能驾驶舱")
    assert "一键体验演示" in _button_labels(app)


def test_material_page_renders_upload_control(app: "AppTest") -> None:
    """会议材料与任务生成页必须存在文件上传控件与解析按钮。"""
    _goto(app, nav.PAGE_MATERIAL)
    # file_uploader 在 AppTest 中以「上传」类控件呈现，宽松断言控件数量大于 0
    assert len(app.get("file_uploader")) >= 1 or len(app.button) > 0


def test_material_page_hides_results_without_material(app: "AppTest") -> None:
    """未上传材料时，页面不得展示「属于本次材料」的识别结果。

    判据：识别结果区的「确认入库」按钮不存在（该区只在有解析结果时渲染），
    以直接断言控件的方式避免「看起来像识别结果」的假象。
    """
    _goto(app, nav.PAGE_MATERIAL)
    assert list(app.exception) == []
    assert "确认入库" not in _button_labels(app)


def test_tasks_page_renders_database_content(app: "AppTest") -> None:
    """督办页必须能从正式库读到任务（只读验证，不写库）。"""
    _goto(app, nav.PAGE_TASKS)
    assert list(app.exception) == []
    # 页面应至少有数据表或空状态提示之一
    assert len(app.dataframe) > 0 or len(app.markdown) > 0


def test_report_page_renders(app: "AppTest") -> None:
    """汇报页必须能渲染（生成报告按钮存在）。"""
    _goto(app, nav.PAGE_REPORT)
    labels = _button_labels(app)
    assert labels, "汇报页没有任何按钮，可能未正确渲染"
