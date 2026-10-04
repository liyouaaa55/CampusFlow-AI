"""表格组件：把 Agent 输出的字典列表转换为展示用 DataFrame 与样式。

约定：
- 只做字段改名与中文标签映射（枚举取自 config/settings.py）；
- 不修改、不派生任何业务字段（风险等级、提醒等级一律原样展示）；
- 颜色只作用于「展示」，不参与任何判断。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import pandas as pd

from config import settings
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

# 界面用状态中文名：在全局配置基础上，把 urgent 显示为「紧急」以贴近督办口径
UI_STATUS_LABELS: dict[str, str] = {**settings.STATUS_LABELS, "urgent": "紧急"}

# 风险等级中文徽章文案（与 PlannerAgent 的 green / yellow / red 对应）
RISK_LABELS: dict[str, str] = dict(styles.RISK_LABELS)

# 未评估时的占位文案
UNKNOWN_TEXT: str = "—"


# ============================================================
# 一、标签映射
# ============================================================


def status_label(status: str | None) -> str:
    """状态中文名（urgent 显示为「紧急」）。"""
    if not status:
        return UNKNOWN_TEXT
    return UI_STATUS_LABELS.get(status, status)


def priority_label(priority: str | None) -> str:
    """优先级中文名（高 / 中 / 低）。"""
    if not priority:
        return UNKNOWN_TEXT
    return settings.PRIORITY_LABELS.get(priority, priority)


def risk_label(risk: str | None) -> str:
    """风险等级中文名（正常 / 需确认 / 高风险）。"""
    key = (risk or "").strip()
    if not key:
        return UNKNOWN_TEXT
    return RISK_LABELS.get(key, key)


def status_options() -> dict[str, str]:
    """状态下拉选项：{中文标签: 枚举值}（顺序与枚举定义一致）。"""
    return {UI_STATUS_LABELS[value]: value for value in settings.TASK_STATUSES}


def priority_options() -> dict[str, str]:
    """优先级下拉选项：{中文标签: 枚举值}。"""
    return {settings.PRIORITY_LABELS[value]: value for value in settings.PRIORITIES}


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


def logs_dataframe(logs: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """操作日志 → DataFrame（动作中文化）。"""
    return pd.DataFrame(
        [
            {
                "操作时间": log.get("created_at") or UNKNOWN_TEXT,
                "动作": "创建" if log.get("action") == "create" else status_label(log.get("action")),
                "操作人": log.get("operator") or UNKNOWN_TEXT,
                "备注": log.get("remark") or UNKNOWN_TEXT,
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
