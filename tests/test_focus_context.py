"""「查看并处理」上下文优化测试（v2.0 P0 产品体验）。

核心约束：当前处置任务优先展示，全部任务保留，不新增 LLM 调用。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from database import sqlite_db
from ui.components import state
from ui.pages import dashboard_page as dp
from ui.pages import tasks_page as tp


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "focus.db"
    sqlite_db.init_db(p)
    return p


def _seed(db_path: Path, count: int = 3) -> list[int]:
    ids: list[int] = []
    for i in range(count):
        created = sqlite_db.create_task(
            {
                "title": f"任务{i}",
                "department": "学工办",
                "deadline": "2099-12-31",
                "risk_level": "green",
            },
            db_path=db_path,
        )
        ids.append(int(created["id"]))
    return ids


# ============================================================
# 一、focus 状态的消费语义
# ============================================================


def test_focus_is_consumed_once() -> None:
    """focus 消费后不可再次读取，避免刷新页面反复跳转。"""
    state.set_focus_task(58)
    assert state.pop_focus_task() == 58
    assert state.pop_focus_task() is None


def test_focus_find_task() -> None:
    """能在任务集合中正确定位目标任务。"""
    tasks = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert tp._find_task(tasks, 2) == {"id": 2}
    assert tp._find_task(tasks, 99) is None
    assert tp._find_task(tasks, None) is None


# ============================================================
# 二、信息架构：当前任务优先，全部任务保留
# ============================================================


def test_render_consumes_focus_before_list() -> None:
    """render() 必须先消费 focus，再渲染任务列表。"""
    source = inspect.getsource(tp.render)
    assert "state.pop_focus_task()" in source
    focus_at = source.index("state.pop_focus_task()")
    filters_at = source.index("_render_filters(all_tasks)")
    assert focus_at < filters_at, "focus 必须在筛选/列表之前消费"


def test_focus_panel_rendered_before_list() -> None:
    """「当前处置任务」必须排在「全部任务」之前。"""
    source = inspect.getsource(tp.render)
    assert source.index("_render_focus_panel(focus_task)") < source.index(
        "_render_filters(all_tasks)"
    )


def test_focus_panel_contains_predispatch_entry() -> None:
    """当前处置任务区直接提供「下发前智能分析」入口。

    v3.0 第二阶段：第三页只负责下发前流程，执行期督办已迁往第四页，
    因此这里同时断言第三页**不再**渲染执行期处置面板。
    """
    source = inspect.getsource(tp._render_focus_panel)
    assert "_render_predispatch_panel" in source
    assert "_render_execution_panel" not in source, "执行期督办不得再出现在第三页"
    assert "scope=\"focus\"" in source or "scope='focus'" in source


def test_all_tasks_section_still_rendered() -> None:
    """全部任务列表必须保留（不得被当前任务模式取代）。"""
    source = inspect.getsource(tp.render)
    assert "_render_filters(all_tasks)" in source
    assert "st.dataframe(tables.tasks_styler(filtered)" in source
    assert "_render_actions(filtered" in source


def test_focus_panel_is_not_a_hidden_state() -> None:
    """当前任务不是「隐藏全部任务」，二者并存。"""
    source = inspect.getsource(tp.render)
    # 既渲染 focus 面板，也渲染全部任务
    assert "_render_focus_panel" in source
    assert "_render_actions" in source
    # 且没有用 return 提前终止整页
    focus_block = source.split("_render_focus_panel(focus_task)")[1]
    assert "return" not in focus_block.split("st.divider()")[0]


def test_focus_task_may_be_missing() -> None:
    """目标任务不存在时不报错、不阻断页面。"""
    assert tp._find_task([{"id": 1}], 999) is None


# ============================================================
# 三、scope 命名空间：避免 widget key 冲突
# ============================================================


def test_execution_panel_scopes_widget_keys() -> None:
    """执行期督办面板的 widget key 必须按 scope 区分。

    v3.0 第三阶段：该面板已从 tasks_page 抽取到 ui/pages/execution_panel.py，
    断言目标随之迁移；key 命名规则（含 scope）保持不变。
    """
    from ui.pages import execution_panel

    source = inspect.getsource(execution_panel.render_execution_panel)
    assert 'scope: str = "inspection"' in source
    for part in ("analyze", "confirm", "cancel", "operator"):
        assert f"cf_exec_{{scope}}_{part}" in source, part


# ============================================================
# 四、零 LLM：当前处置任务区不得触发模型
# ============================================================


def test_focus_panel_does_not_call_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """渲染当前处置任务不调用大模型（分析仅在用户点击按钮时触发）。

    v3.0 第三阶段：执行期分析 run_analysis 已迁往 ui/pages/execution_panel.py；
    这里同时断言第三页渲染路径既不引用它、也不触发任何模型调用。
    """
    from llm import client
    from ui.pages import execution_panel

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("不应调用大模型")

    monkeypatch.setattr(client, "chat_json", boom)
    monkeypatch.setattr(execution_panel, "run_analysis", boom)
    # _render_focus_panel 内部不调用 run_analysis，仅在点击后由按钮触发
    assert "run_analysis" not in inspect.getsource(tp._render_focus_panel)
    # 第三页整体也不再包含执行期分析入口
    assert "run_analysis" not in inspect.getsource(tp)


# ============================================================
# 五、首页入口仍能定位
# ============================================================


def test_dashboard_finding_can_be_focused() -> None:
    """首页「AI 主动发现」的条目可写入 focus 并被督办中心定位。"""
    findings = dp.collect_findings()
    if not findings:
        pytest.skip("当前数据库无可展示的发现项")

    target = findings[0]
    state.set_focus_task(int(target["task_id"]))
    consumed = state.pop_focus_task()
    assert consumed == int(target["task_id"])

    task = sqlite_db.get_task(consumed)
    assert task is not None
    assert tp._find_task([task], consumed) is not None

# ============================================================
# 六、专注模式：消除重复渲染
# ============================================================


def test_focus_mode_skips_duplicate_rendering() -> None:
    """focus 模式下不得重复渲染任务详情与智能处置。

    顶部「当前处置任务」已完整呈现目标任务，_render_actions 只保留切换入口。
    """
    source = inspect.getsource(tp._render_actions)
    guard = "if preselect_id is not None and task_id == int(preselect_id):"
    assert guard in source, "缺少专注模式判定"
    # 判定之后必须 return，确保后续的详情/智能处置/日志不再执行
    tail = source.split(guard)[1]
    assert "return" in tail.split("cards.section(\"任务状态修改\"")[0]


def test_focus_mode_keeps_task_switcher() -> None:
    """专注模式仍必须提供任务切换入口（全部任务管理能力不丢）。"""
    source = inspect.getsource(tp._render_actions)
    assert 'st.selectbox("选择任务"' in source
    assert '"切换任务"' in source


def test_normal_mode_unchanged() -> None:
    """普通模式（preselect_id=None）必须完整渲染操作区。"""
    source = inspect.getsource(tp._render_actions)
    body = source.split('def _render_actions(')[1]
    assert '_render_status_form' in body
    assert '_render_predispatch_panel' in body
    assert '_render_execution_panel' not in body, "执行期督办不得再出现在第三页"
    assert 'sqlite_db.get_logs' in body
    # 专注模式判定必须要求 preselect_id 非空，普通模式不受影响
    assert 'if preselect_id is not None and task_id == int(preselect_id):' in body


def test_focus_mode_still_lists_all_tasks() -> None:
    """专注模式下全部任务表格仍必须渲染。"""
    source = inspect.getsource(tp.render)
    assert 'st.dataframe(tables.tasks_styler(filtered)' in source
    assert '_render_actions(filtered, preselect_id=focus_id)' in source

def test_switch_section_header_precedes_task_selectbox() -> None:
    """专注模式下「切换任务」标题必须渲染在选择器之前。

    否则会出现「选择器悬在区块标题之上」的错位观感：
    标题看起来属于它下方的内容，而不是它要说明的控件。
    """
    source = inspect.getsource(tp._render_actions)
    header_at = source.index('"切换任务"')
    selectbox_at = source.index('st.selectbox("选择任务"')
    assert header_at < selectbox_at, "「切换任务」标题必须先于任务选择器渲染"