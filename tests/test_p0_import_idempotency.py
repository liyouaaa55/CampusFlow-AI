"""2.1 网评冲刺 P0-B：会话级导入幂等性（重复确认不产生重复任务）。

设计边界（经批准的最小方案）
----------------------------
稳定唯一标识 = 「本批待确认识别结果的内容指纹」（规范化全字段 sha256，
**不使用标题单独判定**）。仅阻断**同一会话内同一批内容**的重复确认：
    - 双击 / 重复点击 / 重新载入同一材料 → 指纹一致 → 拒绝，不写任何数据；
    - 任何业务字段变化（人工修正、增删行）→ 指纹不同 → 正常放行；
    - 刷新页面 / 新会话 → 会话指纹失效 → 不永久封禁后续合法导入。
跨会话的「同一材料重复导入」需要持久化导入标识（阶段 B 批次模型），
本轮明确不做——相关测试如实标注该边界。

安全约定：全部使用 tmp_path 隔离数据库；LLM 替换为抛错桩；
AppTest 走真实页面（会议材料与任务生成 → 确认入库）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from database import sqlite_db
from ui.components import state
from ui.pages import nav, review_page

pytest.importorskip("streamlit")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402


# ============================================================
# 夹具与工具
# ============================================================


def _planned(title: str, **overrides: Any) -> dict[str, Any]:
    task = {
        "title": title,
        "department": "学工办",
        "owner": "张老师",
        "deadline": "2099-12-31",
        "priority": "medium",
        "status": "pending",
        "risk_level": "green",
        "need_confirm": False,
        "description": f"{title}的说明",
    }
    task.update(overrides)
    return task


def _edited_frame(titles: list[str]) -> Any:
    """走与页面相同的编辑器数据帧构造（保证列结构一致）。"""
    return review_page._build_editor_frame([_planned(title) for title in titles])


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离数据库：batch_create 走模块全局 DB_PATH，指向 tmp。"""
    path = tmp_path / "import_idem.db"
    sqlite_db.init_db(path)
    monkeypatch.setattr(sqlite_db, "DB_PATH", path)
    return path


def _count_tasks(db: Path) -> int:
    import sqlite3

    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return int(connection.execute("select count(*) from tasks").fetchone()[0])
    finally:
        connection.close()


# ============================================================
# 一、内容指纹（纯函数）
# ============================================================


def test_fingerprint_is_stable_and_content_sensitive() -> None:
    """同一批内容 → 同一指纹；任何业务字段变化 → 不同指纹。"""
    batch = [_planned("甲"), _planned("乙")]

    assert state.compute_import_fingerprint(batch) == state.compute_import_fingerprint(batch)
    # None / 空串 / 纯空白等价（不产生不同指纹）
    sparse = [_planned("甲", owner=None), _planned("乙", owner="   ")]
    dense = [_planned("甲", owner=""), _planned("乙", owner="")]
    assert state.compute_import_fingerprint(sparse) == state.compute_import_fingerprint(dense)

    # 任一业务字段变化 → 不同指纹
    changed_owner = [_planned("甲", owner="王老师"), _planned("乙")]
    assert state.compute_import_fingerprint(changed_owner) != state.compute_import_fingerprint(batch)
    changed_deadline = [_planned("甲", deadline="2099-11-30"), _planned("乙")]
    assert state.compute_import_fingerprint(changed_deadline) != state.compute_import_fingerprint(batch)
    # 增删行 → 不同指纹
    assert state.compute_import_fingerprint([_planned("甲")]) != state.compute_import_fingerprint(batch)


def test_fingerprint_ignores_row_order_and_non_editable_fields() -> None:
    """调序不产生新批次（宁可误拦，不放行重复入库）；非编辑字段不参与。"""
    batch = [_planned("甲"), _planned("乙")]
    reordered = [_planned("乙"), _planned("甲")]
    assert state.compute_import_fingerprint(reordered) == state.compute_import_fingerprint(batch)

    # 风险等级 / 需确认标记由 PlannerAgent 产出、页面不可编辑，不参与指纹
    re_risked = [dict(_planned("甲"), risk_level="red", need_confirm=True), _planned("乙")]
    assert state.compute_import_fingerprint(re_risked) == state.compute_import_fingerprint(batch)


def test_fingerprint_empty_batch_is_empty_string() -> None:
    """空批次返回空串——不与任何已入库批次匹配（防空批被误判为重复）。"""
    assert state.compute_import_fingerprint([]) == ""
    assert state.compute_import_fingerprint(None) == ""


def test_fingerprint_ignores_blank_title_rows() -> None:
    """与 _rows_to_tasks 一致：空标题行被跳过后再计算指纹。"""
    frame = review_page._build_editor_frame([_planned("甲"), dict(_planned(""), title="  ")])
    tasks = review_page._rows_to_tasks(frame)
    assert len(tasks) == 1
    assert state.compute_import_fingerprint(tasks) == state.compute_import_fingerprint([_planned("甲")])


# ============================================================
# 二、_confirm_import：纯编排（守卫 + 入库）
# ============================================================


def test_confirm_import_creates_expected_count(db: Path) -> None:
    """一次确认只创建预期数量的任务。"""
    outcome = review_page._confirm_import(_edited_frame(["甲", "乙"]), last_fingerprint=None)
    assert outcome["ok"] is True
    assert outcome["created_ids"] and len(outcome["created_ids"]) == 2
    assert outcome["fingerprint"]
    assert _count_tasks(db) == 2


def test_confirm_import_rejects_same_batch_twice(db: Path) -> None:
    """同一会话内同一批内容重复确认：拒绝且不写任何数据。"""
    first = review_page._confirm_import(_edited_frame(["甲", "乙"]), last_fingerprint=None)
    assert first["ok"] is True

    second = review_page._confirm_import(
        _edited_frame(["甲", "乙"]), last_fingerprint=first["fingerprint"]
    )
    assert second["ok"] is False
    assert "已入库" in second["reason"]
    assert second["created_ids"] == []
    assert _count_tasks(db) == 2, "重复确认不得新增任务"


def test_confirm_import_allows_modified_content(db: Path) -> None:
    """内容变化（增行）→ 指纹不同 → 正常放行，按新批次整批入库。

    边界（如实标注）：放行后该批次**全部**作为新任务入库（含与上批同名的
    行）——行级增量合并需要持久化批次标识，属阶段 B，本轮明确不做。
    """
    first = review_page._confirm_import(_edited_frame(["甲", "乙"]), last_fingerprint=None)
    assert _count_tasks(db) == 2

    second = review_page._confirm_import(
        _edited_frame(["甲", "乙", "丙"]), last_fingerprint=first["fingerprint"]
    )
    assert second["ok"] is True
    assert _count_tasks(db) == 5, "内容变化视为新批次：3 条全部入库（2 + 3 = 5）"


def test_confirm_import_allows_after_session_reset(db: Path) -> None:
    """会话指纹失效（刷新页面 / 新会话）→ 不永久封禁后续合法导入。"""
    first = review_page._confirm_import(_edited_frame(["甲", "乙"]), last_fingerprint=None)
    assert _count_tasks(db) == 2

    again = review_page._confirm_import(_edited_frame(["甲", "乙"]), last_fingerprint=None)
    assert again["ok"] is True
    assert _count_tasks(db) == 4


def test_confirm_import_rejects_empty_batch(db: Path) -> None:
    """空批次：明确拒绝，不写库。"""
    outcome = review_page._confirm_import(_edited_frame([]), last_fingerprint=None)
    assert outcome["ok"] is False
    assert "没有可入库的任务" in outcome["reason"]
    assert _count_tasks(db) == 0


def test_confirm_import_reports_write_failure(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """入库异常（如校验失败）：转为中文失败原因，且不留下半成品数据。"""

    def _boom(*args: object, **kwargs: object) -> object:
        raise ValueError("截止日期必须为 YYYY-MM-DD 格式")

    monkeypatch.setattr(sqlite_db, "batch_create", _boom)
    outcome = review_page._confirm_import(_edited_frame(["甲"]), last_fingerprint=None)
    assert outcome["ok"] is False
    assert "入库失败" in outcome["reason"]
    assert _count_tasks(db) == 0


# ============================================================
# 三、AppTest 真实页面：确认入库的端到端幂等
# ============================================================


@pytest.fixture()
def app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AppTest 隔离库 + LLM 抛错桩。"""
    path = tmp_path / "app_import.db"
    sqlite_db.init_db(path)
    monkeypatch.setattr(sqlite_db, "DB_PATH", path)

    from llm import client

    def _no_llm(*args: object, **kwargs: object) -> object:
        raise client.LLMError("本测试不调用真实大模型")

    monkeypatch.setattr(client, "chat_json", _no_llm)
    return path


def _open_app() -> AppTest:
    from config import settings

    instance = AppTest.from_file(str(settings.BASE_DIR / "app.py"), default_timeout=60)
    instance.run()
    return instance


def _button_by_label(app: AppTest, label: str) -> Any:
    return next((item for item in app.button if item.label == label), None)


def _open_material_page_with(app: AppTest, titles: list[str]) -> None:
    app.session_state[state.KEY_PARSED_TASKS] = [_planned(title) for title in titles]
    app.session_state[state.KEY_PARSED_FILE] = "测试材料.txt"
    app.session_state["cf_nav_page"] = nav.PAGE_MATERIAL
    app.run()


def test_material_page_confirm_is_idempotent_within_session(app_db: Path) -> None:
    """真实页面：同一会话内重复确认同一批内容，第二次被拒且不新增任务。"""
    app = _open_app()
    _open_material_page_with(app, ["导入甲", "导入乙"])

    button = _button_by_label(app, "确认入库")
    assert button is not None, "有待确认结果时应出现「确认入库」按钮"
    button.click()
    app.run()
    assert _count_tasks(app_db) == 2

    # 模拟同一会话内重新载入同一批材料并再次点击确认
    _open_material_page_with(app, ["导入甲", "导入乙"])
    button2 = _button_by_label(app, "确认入库")
    assert button2 is not None
    button2.click()
    app.run()

    assert _count_tasks(app_db) == 2, "同一会话同一批内容重复确认不得新增任务"
    assert any("已入库" in str(item.value) for item in app.error), "必须给出明确的中文拒绝提示"


def test_material_page_allows_modified_content_in_same_session(app_db: Path) -> None:
    """真实页面：同会话内内容变化后确认，正常放行（按新批次整批入库）。"""
    app = _open_app()
    _open_material_page_with(app, ["导入甲", "导入乙"])
    button = _button_by_label(app, "确认入库")
    assert button is not None
    button.click()
    app.run()
    assert _count_tasks(app_db) == 2

    _open_material_page_with(app, ["导入甲", "导入乙", "导入丙"])
    button2 = _button_by_label(app, "确认入库")
    assert button2 is not None
    button2.click()
    app.run()
    assert _count_tasks(app_db) == 5, "内容变化视为新批次：3 条全部入库（2 + 3 = 5）"


def test_material_page_allows_reimport_in_new_session(app_db: Path) -> None:
    """边界（如实标注）：跨会话的同一材料重复导入会被放行——持久化导入标识
    属阶段 B 批次模型，本轮不做。本测试锁定「不永久封禁」的既定行为。"""
    app1 = _open_app()
    _open_material_page_with(app1, ["导入甲", "导入乙"])
    button = _button_by_label(app1, "确认入库")
    assert button is not None
    button.click()
    app1.run()
    assert _count_tasks(app_db) == 2

    app2 = _open_app()  # 新会话（模拟刷新页面 / 重启后重新打开）
    _open_material_page_with(app2, ["导入甲", "导入乙"])
    button2 = _button_by_label(app2, "确认入库")
    assert button2 is not None
    button2.click()
    app2.run()
    assert _count_tasks(app_db) == 4
