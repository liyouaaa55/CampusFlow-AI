"""页面一：智能驾驶舱（v2.0 首页：AI 主动发现问题 + 今日事务处理入口）。

信息架构（自上而下）
    页面标题
    AI 今日主动检查   —— 总量与关注分类，真实统计
    AI 主动发现       —— 具体事项 + 当前情况 + 处理建议 + 入口
    任务总体情况       —— 既有 KPI（视觉优先级下调）
    部门任务排行       —— 既有统计

数据来源（全部为既有能力，页面不新增任何业务计算）
    reminder_agent.check_all() / count_levels()   提醒等级与剩余天数
    sqlite_db.get_task() / get_logs()             任务字段与历史事件
    execution_tool.select_action()                处置动作（纯函数，**不调用大模型**）
    report_agent.generate_dashboard()             既有 KPI 统计

重要：本页**不会**为任何任务调用大模型。AI 处置动作由 ExecutionAgent 使用的
同一套确定性规则（execution_tool.select_action）给出，因此首页展示的
「处理建议」与用户点进智能处置后看到的建议一致，但不会产生任何 LLM 请求。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import streamlit as st

from agent import reminder_agent, report_agent
from database import closure_store, models, sqlite_db
from llm import client
from tools import execution_tool
from ui import labels
from ui.components import cards, pipeline, state, styles
from ui.pages import nav

# 页面标题与副标题
PAGE_TITLE: str = "智能驾驶舱"
PAGE_SUBTITLE: str = "AI 主动发现问题，协助推进高校基层事务执行"

# 首页最多展示的「AI 主动发现」条数（避免首页过长）
MAX_FINDINGS_SHOWN: int = 5

# 柱状图高度
CHART_HEIGHT: int = 300

# 与「处置动作」相关的事件类型（仅用于陈述事实，不做判断）
DISPOSAL_ACTIONS: frozenset[str] = frozenset(
    {
        models.ACTION_DRAFT,
        models.ACTION_REQUEST,
        models.ACTION_ESCALATE,
        models.ACTION_CONFIRM,
    }
)


# ------------------------------------------------------------
# 发现项的采集：全部确定性计算，零 LLM
# ------------------------------------------------------------


def collect_findings(
    limit: int = MAX_FINDINGS_SHOWN,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """采集需要用户关注的具体事项。

    关注判定完全基于既有数据，不做推断：
      - 提醒等级为 即将到期 / 临期 / 已逾期；
      - 或风险等级为 red（高风险）；
      - 或被标记为需补充信息（need_confirm）；
      - 且任务尚未正式闭环（task_closures 中无记录，与下发门禁同一口径）。

    每条返回：
        task_id / tone / tag / title / situation / advice
    处置动作取自 execution_tool.select_action —— 与 ExecutionAgent 完全
    相同的确定性规则，因此不产生任何 LLM 调用，也不会与实际处置结果矛盾。
    """
    findings: list[dict[str, Any]] = []

    for item in reminder_agent.check_all(db_path=db_path):
        task_id = int(item["task_id"])
        # 正式闭环任务不再纳入督办发现：判定依据是 task_closures 表是否存在记录
        # （与 tools/task_dispatch.dispatch_task 完全同一口径）。
        # **绝不**用 tasks.status == "completed" 代替——闭环不回写 status。
        if closure_store.is_closed(task_id, db_path=db_path):
            continue
        task = sqlite_db.get_task(task_id, db_path=db_path)
        if task is None:
            continue

        level = str(item.get("level") or "")
        risk = str(task.get("risk_level") or "")
        need_confirm = bool(task.get("need_confirm"))
        if level not in execution_tool.DRAFT_NOTIFY_LEVELS and risk != "red" and not need_confirm:
            continue

        logs = sqlite_db.get_logs(int(item["task_id"]), db_path=db_path)
        action = execution_tool.select_action(level, task, logs)

        findings.append({
            "task_id": int(item["task_id"]),
            "tone": _tone_of(level, risk, need_confirm),
            "tag": _tag_of(level, risk, need_confirm),
            "title": str(item.get("title") or ""),
            "situation": _situation_of(item, task, logs),
            "advice": labels.action_label(action) if action else "当前无需处置，维持现有进度",
        })

    # 排序：高风险优先，其次按逾期程度
    order = {"danger": 0, "warning": 1, "info": 2, "muted": 3}
    findings.sort(key=lambda f: (order.get(f["tone"], 9), f["title"]))
    return findings[:limit]

def _tone_of(level: str, risk: str, need_confirm: bool) -> str:
    """状态标签的语义色：沿用既有语义色，不新建色系。"""
    if risk == "red" or level == "overdue":
        return "danger"
    if need_confirm or level == "urgent":
        return "warning"
    return "info"


def _tag_of(level: str, risk: str, need_confirm: bool) -> str:
    """状态标签文案，全部经 ui/labels.py 中文化。"""
    if risk == "red":
        return labels.risk_label(risk)
    if need_confirm:
        return "待补充信息"
    return labels.level_label(level)


def _situation_of(
    item: Mapping[str, Any],
    task: Mapping[str, Any],
    logs: Sequence[Mapping[str, Any]],
) -> str:
    """用可核验的事实描述当前情况，不做推断、不写 AI 文案。"""
    facts: list[str] = []
    level = str(item.get("level") or "")
    days_left = item.get("days_left")

    if level == "overdue" and isinstance(days_left, int):
        facts.append(f"已逾期 {abs(days_left)} 天")
    elif level in execution_tool.DRAFT_NOTIFY_LEVELS and isinstance(days_left, int):
        facts.append(f"剩余 {days_left} 天到期")
    else:
        facts.append(labels.level_label(level))

    if not str(task.get("owner") or "").strip():
        facts.append("负责人未指定")
    if not str(task.get("deadline") or "").strip():
        facts.append("截止时间未指定")

    # 历史事件只陈述事实：是否出现过处置动作
    events = {str(log.get("action") or "") for log in logs}
    if DISPOSAL_ACTIONS & events:
        facts.append("历史已记录处置动作")
    elif len(logs) <= 1:
        facts.append("历史无推进记录")

    return " · ".join(facts)


# 与「处置动作」相关的事件类型（仅用于陈述事实，不做判断）
models_actions: frozenset[str] = frozenset(
    {
        models.ACTION_DRAFT,
        models.ACTION_REQUEST,
        models.ACTION_ESCALATE,
        models.ACTION_CONFIRM,
    }
)

# ------------------------------------------------------------
# 页面渲染
# ------------------------------------------------------------


def render() -> None:
    """渲染智能驾驶舱。

    信息层级：标题 → AI 今日主动检查 → AI 主动发现 → 任务总体情况 → 部门排行
    """
    dashboard = report_agent.generate_dashboard()

    _render_header()

    st.write("")
    _render_ai_check()

    st.write("")
    _render_findings()

    st.write("")
    _render_kpi(dashboard)

    st.write("")
    _render_department_ranking(dashboard)

    st.write("")
    cards.note("数据基于实时任务数据库统计，切换页面自动刷新。")


def _render_header() -> None:
    """标题区：左侧页面标题，右上角「一键体验演示」。"""
    title_col, action_col = st.columns([5, 2], gap="small")

    with title_col:
        cards.page_header(PAGE_TITLE, PAGE_SUBTITLE)

    with action_col:
        st.write("")
        if st.button("一键体验演示", type="primary", use_container_width=True):
            _run_demo()


def _run_demo() -> None:
    """一键体验演示：载入内置会议纪要 → 智能解析 → 跳转事项识别。

    仅编排已有流程，不新增业务规则，也不直接写库。
    """
    text = pipeline.load_demo_document()
    if not text:
        st.error("未找到内置演示文档，请检查知识库模板文件是否完整。")
        return

    planned: list[dict[str, Any]] = []
    with st.status("正在执行一键体验演示…", expanded=True) as status:
        st.markdown(styles.STATUS_MARKER, unsafe_allow_html=True)
        st.markdown(styles.STATUS_ICON_CSS, unsafe_allow_html=True)
        st.write("① 载入内置会议纪要模板")
        st.write("② 文本解析")
        try:
            planned = pipeline.identify_and_plan(text, source_type=pipeline.DEMO_SOURCE_TYPE)
        except client.LLMError as exc:
            status.update(label="演示未完成", state="error")
            st.error(f"调用大模型失败：{exc}")
            return
        except ValueError as exc:
            status.update(label="演示未完成", state="error")
            st.error(f"解析失败：{exc}")
            return
        st.write(f"③ 智能事项识别：识别到 {len(planned)} 项事项")
        status.update(label="识别完成", state="complete")

    state.save_parsed(planned, pipeline.DEMO_FILE_LABEL, text)
    state.set_flash(f"一键体验演示已识别 {len(planned)} 项事项，请确认后入库。")
    state.goto(nav.PAGE_MATERIAL)

def _render_ai_check() -> None:
    """AI 今日主动检查：总量 + 关注分类，数据全部来自真实统计。"""
    counts = reminder_agent.count_levels()
    total = int(counts.get("normal") or 0) + int(counts.get("due_soon") or 0) + \
        int(counts.get("urgent") or 0) + int(counts.get("overdue") or 0)

    high_risk = len(report_agent.generate_dashboard().get("top_departments") or []) and 0
    high_risk = _count_risk("red")
    need_info = _count_need_confirm()
    attention = int(counts.get("due_soon") or 0) + int(counts.get("urgent") or 0) + \
        int(counts.get("overdue") or 0)

    cards.banner(
        "AI 今日主动检查",
        [f"AI 已检查 {total} 项事务，发现 {attention} 项需要关注。"],
    )

    st.write("")
    cards.kpi_row([
        {"label": "已检查事务", "value": total, "desc": "当前任务库中的全部事务"},
        {"label": "高风险", "value": high_risk, "desc": "风险等级已为高风险",
         "color": styles.DANGER},
        {"label": "待补充信息", "value": need_info, "desc": "关键信息缺失，需人工补充",
         "color": styles.WARNING},
        {"label": "需要关注", "value": attention, "desc": "临期与逾期事务合计",
         "color": styles.PRIMARY},
    ])


def _count_risk(risk: str, db_path: str | Path | None = None) -> int:
    """统计指定风险等级的任务数（确定性统计，不调用大模型）。"""
    return sum(1 for t in sqlite_db.list_tasks(db_path=db_path) if str(t.get("risk_level") or "") == risk)


def _count_need_confirm(db_path: str | Path | None = None) -> int:
    """统计需补充信息的任务数。"""
    return sum(1 for t in sqlite_db.list_tasks(db_path=db_path) if t.get("need_confirm"))


def _render_findings() -> None:
    """AI 主动发现：具体事项 + 当前情况 + 处理建议 + 入口。"""
    findings = collect_findings()

    cards.section(
        "AI 主动发现",
        f"共发现 {len(collect_findings(limit=10**6))} 项需要关注，以下展示前 {len(findings)} 项",
    )

    if not findings:
        st.success("当前未发现需要关注的事务，各项任务按计划推进。")
        return

    for idx, finding in enumerate(findings):
        st.markdown(
            styles.finding_html(
                styles.tag_html(finding["tag"], finding["tone"]),
                finding["title"],
                finding["situation"],
                finding["advice"],
            ),
            unsafe_allow_html=True,
        )
        if st.button("查看并处理 →", key=f"cf_go_task_{finding['task_id']}_{idx}"):
            state.set_focus_task(finding["task_id"])
            state.goto(nav.PAGE_TASKS)
        st.write("")

def _render_kpi(dashboard: Mapping[str, Any]) -> None:
    """任务总体情况：保留既有 KPI，视觉优先级低于「AI 主动发现」。"""
    cards.section("任务总体情况", "基于任务数据库的实时统计")

    cards.kpi_row(
        [
            {"label": "总任务", "value": dashboard["total"], "desc": "任务数据库中的任务总数"},
            {
                "label": "完成率",
                "value": f"{dashboard['completion_rate']}%",
                "desc": "已完成 / 总任务",
            },
            {
                "label": labels.level_label("due_soon"),
                "value": dashboard["due_soon"],
                "desc": "剩余 3 天内",
                "color": styles.WARNING,
            },
            {
                "label": labels.level_label("urgent"),
                "value": dashboard["urgent"],
                "desc": "剩余 0~1 天",
                "color": "#EA580C",
            },
            {
                "label": labels.level_label("overdue"),
                "value": dashboard["overdue"],
                "desc": "已超过截止时间",
                "color": styles.DANGER,
            },
        ]
    )


def _render_department_ranking(dashboard: Mapping[str, Any]) -> None:
    """部门任务排行（仅柱状图，表格信息重复已移除）。"""
    cards.section("部门执行情况", "按任务数量排序")

    top_departments = dashboard["top_departments"]
    if not top_departments:
        st.caption("暂无部门数据。")
        return

    frame = pd.DataFrame(list(top_departments)).rename(
        columns={"department": "部门", "count": "任务数"}
    )
    st.bar_chart(frame.set_index("部门"), color=styles.PRIMARY, height=CHART_HEIGHT)