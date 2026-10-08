# CampusFlow AI 2.0 Stability Baseline

版本：v2.0-final-rc1（候选，未打 tag）
当前日期：2026-10-08
Git branch：v2.0
Git commit：ca71140（chore: CampusFlow AI 初始导入）

> **重要**：2.0 的全部开发尚未提交，HEAD 仍停留在初始导入。
> 建立本基线的前提是先把当前工作区固化为一次提交（见文末「基线建立方式」）。

## 自动化测试

```
806 passed, 1 skipped, 1 warning
```

| 指标 | 值 |
|---|---|
| 总数 | 807 |
| passed | 806 |
| failed | 0 |
| skipped | 1（test_task_agent 条件跳过，原有） |
| warnings | 1（Altair 弃用提示，原有） |

## 核心 Agent

| Agent | 调用大模型 | 职责 | 文件最后修改 |
|---|---|---|---|
| TaskAgent | 是 | 非结构化材料 → 结构化事项 | 2026-10-07 20:42 |
| PlannerAgent | 否 | 优先级兜底 / 风险等级 / 需确认标记 | 2026-09-23 22:18（未改） |
| ReminderAgent | 否 | 提醒等级判定（即将到期/临期/已逾期） | 2026-09-23 22:26（未改） |
| ExecutionAgent | 是 | 处置分析（situation / reasons） | 2026-10-05 21:34 |
| ReportAgent | 否 | 统计与报告 | 2026-10-05 22:11 |

配套确定性层：
- `tools/ledger_parser.py` — 台账逐行确定性解析（不经 LLM）
- `tools/directional_guard.py` — 方向性表述守卫
- `tools/rescue.py` — 原文证据补救层
- `tools/execution_tool.py` — 动作选择 / 七层校验 / 受控执行

## 真实材料

16 份，位于 `C:\Users\liyou\Desktop\测试数据`（4 个场景目录）。
Office 临时文件（`~$`）0 个，已全部排除。

### 黄金回归结果（本轮实测）

| 材料 | 项数 | 状态分布 | 重复 |
|---|---:|---|---|
| 1 实验室安全专项检查 | 6 | pending 6 | 无 |
| 2 校园消防安全检查 | 6 | pending 6 | 无 |
| **3 安全隐患整改台账** | **12** | **processing 7 / pending 3 / completed 2** | 无 |
| 4 极端天气保障 | 14 | pending 14 | 无 |
| 5 运动会筹备工作安排 | 8 | pending 8 | 无 |
| 6 运动会宣传活动 | 8 | pending 8 | 无 |
| **7 运动会筹备任务一览表** | **18** | **completed 3 / processing 5 / pending 10** | 无 |
| **8 运动会进展情况** | **5** | **processing 3 / pending 2** | 无 |
| 9 综合工作会议纪要 | 6 | pending 6 | 无 |
| 10 会议纪要 | 3 | pending 3 | 无 |
| 11 TXT 工作通知 | 6 | pending 6 | 无 |
| 12 Excel 任务安排表 | 10 | pending 10 | 无 |
| **13 学工台账** | **10** | **processing 2 / pending 7 / completed 1** | 无 |
| 14 学生工作处通知 | 3 | pending 3 | 无 |
| 15 工作提示 | 1 | pending 1 | 无 |
| 16 毕业生就业工作安排 | 5 | pending 5 | 无 |

**材料 3 / 7 / 13 五次零波动**（确定性路径，各 5 次结果完全一致）。
**材料 8 方向性泄漏 = 0**。

## 关键 UI

智能驾驶舱 / 会议材料上传 / 事项智能识别 / 任务督办中心 / 执行情况汇报

## 关键交互

| 交互 | 自动化覆盖 | 结果 |
|---|---|---|
| 上传解析（`_run_pipeline` 真实回调） | 是（真实 Streamlit runtime） | 通过 |
| 确认入库 | 部分（preflight 业务链路 4/4 PASS） | 通过 |
| 查看并处理（跳转 + 预选） | 是 | 通过 |
| 专注模式去重 | 是 | 通过 |
| 切换任务 | 是 | 通过 |
| 开始智能分析 | 是（真实 LLM） | 通过 |
| 确认执行 | 函数层已覆盖（P0-3/0-4 轮） | 见「已知限制」 |
| DRAFT_NOTIFY / REQUEST_INFO / ESCALATE | 函数层已覆盖 | 见「已知限制」 |
| 一键体验演示 | 部分（按钮回调含 LLM，未自动化） | **需人工验收** |
| 汇报筛选 / 重新生成 | 是 | 通过 |

## 历史 P0 回归

| 事故 | 根因分类 | 当前状态 |
|---|---|---|
| DISPOSAL_ACTIONS NameError | **C 旧 Streamlit 进程**（非代码缺陷） | 已确认定义先于使用，冷启动正常 |
| STATUS_MARKER AttributeError | **A 代码真的回退**（git checkout 丢失未提交改动） | 已修复，并加 styles contract 测试防复发 |
| 已完成→待办 | **G 业务逻辑回归**（status 写死） | 已修复 |
| 台账 completed 行丢失 | **A 代码缺陷**（依赖 LLM 抽取表格行） | 已改为确定性解析 |
| 方向性表述误拆 | **G 业务逻辑** | 已加守卫 |
| focus_task 重复渲染 | **F 新代码改变共享模块** | 已修复 |
| check / 自检复发 | 未复发 | preflight 30 项 28 PASS / 2 WARN / 0 FAIL |

## 已知限制（不影响比赛版本）

1. **多文件解析未实现** —— 当前为单文件解析，本轮不扩展。
2. **无 OCR** —— 扫描件与图片不支持。
3. **无真实消息发送** —— DRAFT_NOTIFY 仅生成提醒草稿内容，不发送微信/邮件/短信。
4. **PDF 解析告警** —— preflight 中 PDF 项为 WARN（pdfplumber 可用但无中文字形校验）。
5. **大模型 API 连通性告警** —— preflight 中为 WARN（自检时刻的连通性探测，非阻断）。
6. **生产库存在测试残留** —— 交互验证过程在 `data/campusflow.db` 写入了事务事件
   （近 24h：analyze 19 / confirm 5 / escalate 2 / draft 2 / request 1），
   当前 81 条任务中有 4 条含 v2.0 事务事件。**比赛前建议清理或换用演示库**。
7. **一键体验演示未自动化** —— AppTest 无法可靠断言其 LLM 回调结果，需人工验收。
8. **`.xls` 真 BIFF 格式** —— 缺 xlrd，需转存为 .xlsx。
9. **LLM 输出跨请求不确定** —— temperature=0 下仍存在波动，首页与解析链已用确定性层
   （台账解析 / 方向守卫 / 补救层）削弱其影响。