"""中文化一致性测试：内部英文枚举 → 面向用户的中文显示。

本文件是「显示层中文化」的防回归护栏，锁定三件事：

1. **完整**：models 里每一个内部枚举值都有对应的中文标签，没有遗漏；
2. **不泄漏**：取不到映射时返回占位符「—」而非英文原值，
   因此新增枚举若忘记配中文，页面不会漏出英文；
3. **不漂移**：report_tool 的静态兜底表与 knowledge/rules/reminder.json
   的权威标签完全一致，避免报告与界面各说一套。

这些测试同时也是「内部枚举不可随意改名」的保险丝：
一旦有人改动 database.models 的枚举而未同步中文标签，本文件会直接失败。
"""

from __future__ import annotations

import re

from agent import reminder_agent
from config import settings
from database import models
from tools import report_tool
from ui import labels
from ui.components import styles, tables

# 界面上绝对不允许出现的内部英文枚举值（提示词 / 调试场景除外）
FORBIDDEN_DISPLAY_ENUMS: tuple[str, ...] = (
    *(action for action in models.LOG_ACTIONS),
    *(level for level in reminder_agent.REMINDER_LEVELS),
    *(risk for risk in models.RISK_LEVELS),
)


# ============================================================
# 一、标签完整性：每个内部枚举都必须有中文
# ============================================================


def test_all_log_actions_have_chinese_labels() -> None:
    """每个日志动作（含 6 个事务事件）都必须有中文标签。"""
    for action in models.LOG_ACTIONS:
        label = labels.log_action_label(action)
        assert label, f"{action} 缺少中文标签"
        assert label != action, f"{action} 的中文标签不能是英文原值"
        assert re.search(r"[一-鿿]", label), f"{action} 的标签「{label}」不含中文"


def test_all_risk_levels_have_chinese_labels() -> None:
    """三个风险等级都必须有中文标签，且为低/中/高风险。"""
    assert labels.RISK_LABELS == {
        "green": "低风险",
        "yellow": "中风险",
        "red": "高风险",
    }
    for risk in models.RISK_LEVELS:
        assert labels.risk_label(risk) != risk


def test_all_task_statuses_have_chinese_labels() -> None:
    """任务状态全部可中文化。"""
    for status in settings.TASK_STATUSES:
        assert re.search(r"[一-鿿]", labels.status_label(status))


def test_all_priorities_have_chinese_labels() -> None:
    """优先级全部可中文化。"""
    for level in settings.PRIORITIES:
        assert re.search(r"[一-鿿]", labels.priority_label(level))


def test_all_reminder_levels_have_chinese_labels() -> None:
    """提醒等级全部可中文化，且与需求约定的中文名一致。

    注意 due_soon 与 urgent 的中文名是「即将到期」与「临期」，
    与旧版（临期 / 紧急）不同，此处锁定为需求口径。
    """
    expected = {
        "normal": "正常",
        "due_soon": "即将到期",
        "urgent": "临期",
        "overdue": "已逾期",
    }
    for level, want in expected.items():
        assert labels.level_label(level) == want, f"{level} 应显示为「{want}」"


# ============================================================
# 二、不泄漏英文：未映射值一律显示占位符
# ============================================================


def test_unknown_values_render_as_placeholder() -> None:
    """未映射的内部值显示为「—」，绝不把英文原值送到界面上。"""
    for getter, arg in (
        (labels.log_action_label, "never_defined_action"),
        (labels.risk_label, "purple"),
        (labels.status_label, "archived"),
        (labels.priority_label, "P9"),
    ):
        rendered = getter(arg)  # type: ignore[arg-type]
        assert rendered == labels.UNKNOWN_TEXT
        assert arg not in rendered


def test_empty_values_render_as_placeholder() -> None:
    """空值 / None 走占位符，不报错也不漏英文。"""
    for value in (None, "", "   "):
        assert labels.log_action_label(value) == labels.UNKNOWN_TEXT
        assert labels.risk_label(value) == labels.UNKNOWN_TEXT
        assert labels.status_label(value) == labels.UNKNOWN_TEXT
        assert labels.priority_label(value) == labels.UNKNOWN_TEXT
        assert labels.level_label(value) == labels.UNKNOWN_TEXT


def test_no_display_mapping_leaks_english() -> None:
    """遍历所有内部枚举，断言渲染结果中不含英文原值。"""
    for value in FORBIDDEN_DISPLAY_ENUMS:
        for rendered in (
            labels.log_action_label(value),
            labels.risk_label(value) if value in models.RISK_LEVELS else None,
            labels.status_label(value) if value in settings.TASK_STATUSES else None,
            labels.priority_label(value) if value in settings.PRIORITIES else None,
            labels.level_label(value) if value in reminder_agent.REMINDER_LEVELS else None,
        ):
            if rendered is None:
                continue
            assert rendered != value, f"{value} 被原样输出到界面"


# ============================================================
# 三、防漂移：静态兜底表与权威来源必须一致
# ============================================================


def test_report_tool_fallback_matches_reminder_json() -> None:
    """report_tool 的静态兜底标签必须与 reminder.json 完全一致。

    report_tool 属于纯渲染工具、刻意不依赖 agent 层，因此保留了一份静态副本
    以便知识库缺失时仍能出报告。这条测试保证两份定义不会各自漂移。
    """
    authoritative = {
        level: reminder_agent.level_label(level)
        for level in reminder_agent.REMINDER_LEVELS
    }
    assert report_tool.DEFAULT_LEVEL_LABELS == authoritative


def test_overview_metrics_use_authoritative_labels() -> None:
    """报告概况表的等级行标题必须取自 reminder.json，而不是硬编码。"""
    from agent import report_agent  # 局部导入避免与上方 import 顺序耦合

    stats = {
        "total": 6,
        "completed": 2,
        "processing": 1,
        "completion_rate": 33.3,
        "normal": 2,
        "due_soon": 2,
        "urgent": 1,
        "overdue": 1,
    }
    metrics = report_agent._overview_metrics(stats)
    for level in ("due_soon", "urgent", "overdue"):
        assert reminder_agent.level_label(level) in metrics


def test_risk_row_backgrounds_cover_every_risk_label() -> None:
    """整行背景色必须覆盖全部风险中文标签，否则高风险行会丢失底色。"""
    for label in labels.RISK_LABELS.values():
        assert label in styles.RISK_ROW_BACKGROUNDS, f"缺少 {label} 的行背景色"


# ============================================================
# 四、日志表格：动作列走映射，未映射不漏英文
# ============================================================


def test_logs_dataframe_renders_chinese_actions() -> None:
    """日志表格把全部 13 种动作渲染为中文。"""
    frame = tables.logs_dataframe(
        [{"action": action, "operator": "张老师", "created_at": "2026-10-05 09:00:00"} for action in models.LOG_ACTIONS]
    )
    rendered = list(frame["动作"])
    assert len(rendered) == len(models.LOG_ACTIONS)
    for value in rendered:
        assert value != labels.UNKNOWN_TEXT
        assert re.search(r"[一-鿿]", value)
    # 6 个事务事件的中文名必须与需求一致
    mapping = dict(zip(models.LOG_ACTIONS, rendered))
    assert mapping[models.ACTION_ANALYZE] == "智能分析"
    assert mapping[models.ACTION_DRAFT] == "生成提醒草稿"
    assert mapping[models.ACTION_REQUEST] == "请求补充信息"
    assert mapping[models.ACTION_CONFIRM] == "人工确认"
    assert mapping[models.ACTION_REJECT] == "人工否决"
    assert mapping[models.ACTION_ESCALATE] == "提级督办"


def test_logs_dataframe_unknown_action_shows_placeholder() -> None:
    """未知动作在表格里显示「—」，不把英文原值露给用户。"""
    frame = tables.logs_dataframe([{"action": "brand_new_action"}])
    assert list(frame["动作"]) == [labels.UNKNOWN_TEXT]


def test_logs_dataframe_truncates_long_remark() -> None:
    """超长备注被截断，避免撑爆日志表格。"""
    frame = tables.logs_dataframe([{"action": models.ACTION_ANALYZE, "remark": "长" * 300}])
    remark = str(frame["备注"].iloc[0])
    assert len(remark) <= tables.LOG_REMARK_MAX_LENGTH
    assert remark.endswith("…")


# ============================================================
# 五、Prompt 中文化
# ============================================================


def test_execution_prompts_require_chinese_output() -> None:
    """ExecutionAgent 的 Prompt 必须明确要求输出内容为中文。"""
    from llm import prompts

    system = prompts.EXECUTION_ANALYSIS_SYSTEM
    assert "简体中文" in system
    # 字段名保持英文（内部协议），但内容要求中文
    assert "situation" in system and "reasons" in system
    assert "英文" in system
    # 给出中文输出示例，降低模型返回英文的概率
    assert '{"situation"' in system


def test_execution_prompts_keep_english_field_names() -> None:
    """JSON 字段名必须保持英文，不得中文化（否则会破坏解析与测试）。

    字段名的定义出现在 SYSTEM Prompt 的「输出格式」段；
    USER Prompt 只是数据占位符，不负责声明字段名。
    """
    from llm import prompts

    system = prompts.EXECUTION_ANALYSIS_SYSTEM
    assert "situation" in system
    assert "reasons" in system
    # 不得把字段名写成中文
    assert "「情况」" not in system
    assert "「依据」" not in system
    # USER 模板只提供数据，不应把字段名中文化
    assert "「情况」" not in prompts.EXECUTION_ANALYSIS_USER
    assert "「依据」" not in prompts.EXECUTION_ANALYSIS_USER


def test_execution_prompts_forbid_action_fields() -> None:
    """Prompt 仍必须禁止模型输出动作与受保护字段（中文化不得削弱这条约束）。"""
    from llm import prompts

    system = prompts.EXECUTION_ANALYSIS_SYSTEM
    for forbidden in ("action", "params", "need_confirm", "risk_level", "status"):
        assert forbidden in system
