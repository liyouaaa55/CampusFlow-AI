# 任务（事项）模板

> 说明：本文件是「事项」的统一字段说明，与 TaskAgent / PlannerAgent / 数据库 tasks 表一一对应。
> 用于人工补录任务，或作为接口联调时的字段参考。

## 一、识别阶段字段（TaskAgent 输出）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 是 | 任务标题，动词 + 对象，建议 ≤ 30 字。标题为空的事项会被丢弃。 |
| `department` | string | 否 | 责任部门，只能取自原文明确出现的部门名称。 |
| `owner` | string | 否 | 负责人，只能取自原文明确出现的姓名或称呼。 |
| `deadline` | string | 否 | 截止时间，格式 `YYYY-MM-DD`；无法确定时为 `null`。 |
| `priority` | string | 否 | `high` / `medium` / `low`；原文无法判断时为 `null`，由 PlannerAgent 按规则兜底。 |
| `status` | string | 是 | 识别阶段固定为 `pending`。 |
| `description` | string | 否 | 一句话说明「要做什么」。 |

## 二、规划阶段字段（PlannerAgent 追加）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `risk_level` | string | `green` 齐备 / `yellow` 缺负责人或优先级 / `red` 缺截止时间或已逾期。 |
| `need_confirm` | bool | 负责人、截止时间、优先级任一缺失时为 `true`。 |
| `suggestions` | string[] | 针对缺失项与风险的处置建议。 |

## 三、状态取值（仅允许 6 种）

| 状态 | 含义 |
| --- | --- |
| `pending` | 待办（识别后默认） |
| `confirmed` | 人工已确认 |
| `processing` | 进行中 |
| `urgent` | 临期 |
| `completed` | 已完成 |
| `overdue` | 已逾期 |

## 四、人工补录示例（JSON）

```json
[
  {
    "title": "完成宿舍消防隐患专项排查",
    "department": "保卫处",
    "owner": "王老师",
    "deadline": "2026-09-30",
    "priority": "high",
    "status": "pending",
    "description": "建立整改台账并完成隐患销号"
  }
]
```
