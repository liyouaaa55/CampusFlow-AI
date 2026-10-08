"""表格组件：把 Agent 输出的字典列表转换为展示用 DataFrame 与样式。

约定：
- 只做字段改名与中文标签映射（枚举取自 config/settings.py）；
- 不修改、不派生任何业务字段（风险等级、提醒等级一律原样展示）；
- 颜色只作用于「展示」，不参与任何判断。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import pandas as pd

from ui import labels
from ui.components import styles

# ---------- 任务表列名 ----------
COL_TITLE: str = "标题"
COL_STATUS: str = "状态"
COL_OWNER: str = "负责人"
COL_DEPARTMENT: str = "部门"
COL_DEADLINE: str = "截止时间"
COL_PRIORITY: str = "优先级"
COL_RISK: str = "风险等级"
COL_CONFIRM: str = "需人工确认"

TASK_TABLE_COLUMNS: tuple[str, ...] = (
    COL_TITLE,
    COL_STATUS,
    COL_OWNER,
    COL_DEPARTMENT,
    COL_DEADLINE,
    COL_PRIORITY,
    COL_RISK,
    COL_CONFIRM,
)

# ---------- 日志表列名 ----------
LOG_COLUMNS: tuple[str, ...] = ("操作时间", "动作", "操作人", "备注")

# 界面用状态中文名：在全局配置基础上，把 urgent 显示为「临期」以贴近督办口径
UI_STATUS_LABELS: dict[str, str] = dict(labels.STATUS_LABELS_DISPLAY)

# 风险等级中文徽章文案（与 PlannerAgent 的 green / yellow / red 对应）
RISK_LABELS: dict[str, str] = dict(styles.RISK_LABELS)

# 未评估时的占位文案
UNKNOWN_TEXT: str = labels.UNKNOWN_TEXT


# ============================================================
# 一、标签映射
#
# 全部转发到 ui.labels —— 显示层中文映射的唯一来源。
# 本模块保留同名薄封装，仅为维持既有调用点不变。
# ============================================================


def status_label(status: str | None) -> str:
    """状态中文名（urgent 显示为「临期」）。"""
    return labels.status_label(status)


def priority_label(priority: str | None) -> str:
    """优先级中文名（高 / 中 / 低）。"""
    return labels.priority_label(priority)


def log_action_label(action: str | None) -> str:
    """日志动作中文名（含 v2.0 事务事件：智能分析 / 提级督办 等）。

    取不到映射时返回占位符「—」而非原始英文，避免内部枚举泄漏到界面。
    """
    return labels.log_action_label(action)


def risk_label(risk: str | None) -> str:
    """风险等级中文名（低风险 / 中风险 / 高风险）。"""
    return labels.risk_label(risk)


def status_options() -> dict[str, str]:
    """状态下拉选项：{中文标签: 枚举值}（顺序与枚举定义一致）。"""
    return {UI_STATUS_LABELS[value]: value for value in labels.STATUS_ORDER}


def priority_options() -> dict[str, str]:
    """优先级下拉选项：{中文标签: 枚举值}。"""
    return {labels.PRIORITY_LABELS[value]: value for value in labels.PRIORITY_ORDER}


def risk_options() -> dict[str, str]:
    """风险等级下拉选项：{中文标签: 枚举值}。"""
    return {label: value for value, label in RISK_LABELS.items()}


# ============================================================
# 二、任务表
# ============================================================


def tasks_dataframe(tasks: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """任务列表 → 展示用 DataFrame（全部中文化）。"""
    return pd.DataFrame(
        [
            {
                COL_TITLE: task.get("title") or UNKNOWN_TEXT,
                COL_STATUS: status_label(task.get("status")),
                COL_OWNER: task.get("owner") or UNKNOWN_TEXT,
                COL_DEPARTMENT: task.get("department") or UNKNOWN_TEXT,
                COL_DEADLINE: task.get("deadline") or UNKNOWN_TEXT,
                COL_PRIORITY: priority_label(task.get("priority")),
                COL_RISK: risk_label(task.get("risk_level")),
                COL_CONFIRM: "是" if task.get("need_confirm") else "否",
            }
            for task in tasks
        ],
        columns=list(TASK_TABLE_COLUMNS),
    )


def tasks_styler(tasks: Sequence[Mapping[str, Any]]) -> Any:
    """任务表样式：高风险整行浅红、需确认整行浅黄、正常白底。"""
    frame = tasks_dataframe(tasks)
    if frame.empty:
        return frame

    styler = frame.style.apply(_row_background, axis=1)
    styler = styler.map(_risk_cell_style, subset=[COL_RISK])
    return styler.set_table_styles(
        [
            {
                "selector": "th",
                "props": [
                    ("background-color", "#EFF6FF"),
                    ("color", styles.TEXT_MAIN),
                    ("font-weight", "600"),
                    ("text-align", "left"),
                ],
            },
            {"selector": "td", "props": [("text-align", "left")]},
        ]
    )


def _row_background(row: pd.Series) -> list[str]:
    """按风险等级给整行着色。"""
    background = styles.RISK_ROW_BACKGROUNDS.get(str(row.get(COL_RISK)), styles.SURFACE)
    return [f"background-color:{background}"] * len(row)


def _risk_cell_style(value: Any) -> str:
    """风险等级单元格文字着色加粗。"""
    color = styles.RISK_COLORS.get(
        {label: key for key, label in RISK_LABELS.items()}.get(str(value), ""),
        styles.TEXT_SUB,
    )
    return f"color:{color}; font-weight:700"


# ============================================================
# 三、日志表
# ============================================================


LOG_REMARK_MAX_LENGTH: int = 60


def _truncate_remark(remark: Any) -> str:
    """日志备注 → 表格显示文本（限长；空值走占位符）。"""
    text = str(remark or "").strip()
    if not text:
        return UNKNOWN_TEXT
    if len(text) <= LOG_REMARK_MAX_LENGTH:
        return text
    return text[: LOG_REMARK_MAX_LENGTH - 1] + "…"


def logs_dataframe(logs: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """操作日志 → DataFrame（动作中文化）。

    动作列统一走 log_action_label()：同时覆盖状态类动作（pending / completed …）
    与 v2.0 事务事件（analyze / escalate …），未映射时返回「—」而非英文原值。
    备注列做长度截断，避免 analyze 事件的长摘要撑爆日志表格。
    """
    return pd.DataFrame(
        [
            {
                "操作时间": log.get("created_at") or UNKNOWN_TEXT,
                "动作": log_action_label(log.get("action")),
                "操作人": log.get("operator") or UNKNOWN_TEXT,
                "备注": _truncate_remark(log.get("remark")),
            }
            for log in logs
        ],
        columns=list(LOG_COLUMNS),
    )


# ============================================================
# 四、下拉选项文案
# ============================================================


def task_option_label(task: Mapping[str, Any]) -> str:
    """任务下拉选项文案：`#1 宿舍消防隐患排查（保卫处 / 王老师）`。"""
    return "#{id} {title}（{department} / {owner}）".format(
        id=task.get("id"),
        title=task.get("title") or "未命名任务",
        department=task.get("department") or "未指定部门",
        owner=task.get("owner") or "未指定负责人",
    )
