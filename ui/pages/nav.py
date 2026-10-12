"""页面名称的唯一来源（v3.0 第一阶段）。

为什么单独一个模块：
    页面名既被 app.py 用于构建侧边栏导航，也被各页面用于 state.goto() 跳转，
    还被测试用于断言导航清单。把名称集中在这里，避免「改名后某个 goto 静默失效」
    （state.goto 会校验目标页是否在 PAGES 内，名字写错会被直接丢弃）。

顺序即侧边栏顺序：
    智能驾驶舱 → 会议材料与任务生成 → 任务督办中心 → 执行反馈与检查 → 工作检查报告

v3.0 变更记录：
    第一阶段：原「会议材料上传」与「事项智能识别」合并为「会议材料与任务生成」
             （上传、解析、识别、修正、确认生成任务在同一页完成）。
    第二阶段：正式锁定五页名称与职责——
             「执行检查」→「执行反馈与检查」（承担执行反馈、附件、状态更新与持续督办）；
             「执行情况汇报」→「工作检查报告」（正式文字总结 + 完整任务明细表）。
"""

from __future__ import annotations

PAGE_DASHBOARD: str = "智能驾驶舱"
PAGE_MATERIAL: str = "会议材料与任务生成"
PAGE_TASKS: str = "任务督办中心"
PAGE_INSPECTION: str = "执行反馈与检查"
PAGE_REPORT: str = "工作检查报告"

PAGES: tuple[str, ...] = (
    PAGE_DASHBOARD,
    PAGE_MATERIAL,
    PAGE_TASKS,
    PAGE_INSPECTION,
    PAGE_REPORT,
)
