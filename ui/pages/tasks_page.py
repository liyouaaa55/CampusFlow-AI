"""页面四：任务督办中心。

- 数据来源：database.sqlite_db.list_tasks()（支持状态 / 部门筛选）
- 状态修改：database.sqlite_db.update_status()（自动写入操作日志）
- 日志查看：database.sqlite_db.get_logs()
优先级与风险等级、负责人关键词为展示层筛选（不改变业务接口）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import streamlit as st

from database import sqlite_db
from ui.components import cards, state, styles, tables

# 页面标题与副标题
PAGE_TITLE: str = "任务督办中心"
PAGE_SUBTITLE: str = "查看任务、更新状态、全程留痕"

# 筛选下拉的「全部」选项
ALL_OPTION: str = "全部"


def render() -> None:
    """渲染任务督办中心。"""
    cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)

    flash = state.pop_flash()
    if flash:
        st.success(flash)

    all_tasks = sqlite_db.list_tasks()
    if not all_tasks:
        if cards.empty_state(
            "暂无任务记录",
            "请先上传会议纪要或任务台账，识别确认后写入库。",
            "前往会议材料上传",
        ):
            state.goto("会议材料上传")
        return

    filters = _render_filters(all_tasks)
    filtered = _apply_filters(filters)

    cards.section(
        f"任务列表（{len(filtered)} / {len(all_tasks)} 条）",
        "高风险浅红、需确认浅黄、正常白底",
    )
    if not filtered:
        st.warning("当前筛选条件下没有任务。")
        return

    st.dataframe(tables.tasks_styler(filtered), hide_index=True, use_container_width=True)

    st.divider()
    _render_actions(filtered)


# ------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------


def _render_filters(all_tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """渲染筛选区，返回筛选条件。"""
    departments = sorted({str(task["department"]) for task in all_tasks if task.get("department")})

    status_col, department_col, owner_col = st.columns([1, 1, 1])
    status_label = status_col.selectbox("状态", [ALL_OPTION, *tables.status_options()])
    department_label = department_col.selectbox("部门", [ALL_OPTION, *departments])
    owner_keyword = owner_col.text_input("负责人（关键词）", placeholder="支持模糊匹配")

    priority_col, risk_col = st.columns([1, 1])
    priority_label = priority_col.selectbox("优先级", [ALL_OPTION, *tables.priority_options()])
    risk_label = risk_col.selectbox("风险等级", [ALL_OPTION, *tables.risk_options()])

    return {
        "status": tables.status_options().get(status_label),
        "department": None if department_label == ALL_OPTION else department_label,
        "owner": owner_keyword.strip(),
        "priority": tables.priority_options().get(priority_label),
        "risk": tables.risk_options().get(risk_label),
    }


def _apply_filters(filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    """状态 / 部门下推到 list_tasks()，其余条件在展示层过滤。"""
    tasks = sqlite_db.list_tasks(status=filters["status"], department=filters["department"])

    owner_keyword = str(filters["owner"] or "")
    if owner_keyword:
        tasks = [task for task in tasks if owner_keyword in str(task.get("owner") or "")]

    priority = filters["priority"]
    if priority:
        tasks = [task for task in tasks if task.get("priority") == priority]

    risk = filters["risk"]
    if risk:
        tasks = [task for task in tasks if task.get("risk_level") == risk]

    return tasks


def _render_actions(tasks: Sequence[Mapping[str, Any]]) -> None:
    """任务操作区：任务详情 + 状态修改卡片 + 操作日志。"""
    cards.section("任务状态修改", "状态变更会自动写入操作日志")

    options = {tables.task_option_label(task): task for task in tasks}
    selected_label = st.selectbox("选择任务", list(options))
    task = options[selected_label]
    task_id = int(task["id"])

    detail_col, form_col = st.columns([11, 9], gap="large")

    with detail_col:
        _render_detail(task)

    with form_col:
        _render_status_form(task, task_id)

    st.write("")
    cards.section("操作日志", "状态变更全程留痕")
    logs = sqlite_db.get_logs(task_id)
    if logs:
        st.dataframe(tables.logs_dataframe(logs), hide_index=True, use_container_width=True)
    else:
        st.caption("该任务暂无操作日志。")


def _render_detail(task: Mapping[str, Any]) -> None:
    """任务详情卡片（含中文风险徽章）。"""
    risk = str(task.get("risk_level") or "")
    badge = styles.risk_badge_html(risk)

    rows = [
        ("当前状态", tables.status_label(task.get("status"))),
        ("负责人", task.get("owner") or "未指定"),
        ("责任部门", task.get("department") or "未指定"),
        ("截止时间", task.get("deadline") or "未指定"),
        ("优先级", tables.priority_label(task.get("priority"))),
    ]
    body = "".join(
        f'<div class="cf-file-row"><span class="cf-file-label">{label}</span><span>{value}</span></div>'
        for label, value in rows
    )
    st.markdown(
        f'<div class="cf-card"><div class="cf-file-head" style="color:{styles.TEXT_MAIN}">'
        f"<span>任务详情</span>{badge}</div>{body}</div>",
        unsafe_allow_html=True,
    )


def _render_status_form(task: Mapping[str, Any], task_id: int) -> None:
    """状态修改表单（提交后调用 update_status 并留痕）。"""
    status_map = tables.status_options()
    labels = list(status_map)
    current = tables.status_label(task.get("status"))
    index = labels.index(current) if current in labels else 0

    with st.form(key=f"cf_status_form_{task_id}"):
        new_label = st.selectbox("新状态", labels, index=index)
        operator = st.text_input("操作人", value="AI")
        remark = st.text_input("备注", value="")
        submitted = st.form_submit_button("更新任务状态", type="primary", use_container_width=True)

        if submitted:
            try:
                sqlite_db.update_status(
                    task_id,
                    status_map[new_label],
                    operator=operator.strip() or "AI",
                    remark=remark.strip(),
                )
            except (ValueError, sqlite_db.TaskNotFoundError) as exc:
                st.error(f"状态更新失败：{exc}")
            else:
                state.set_flash(f"任务 #{task_id} 状态已更新为「{new_label}」。")
                st.rerun()
