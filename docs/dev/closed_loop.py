"""开发期脚本：闭环链路验证（规划 → 入库 → 状态流转 → 提醒 → 报告）。

仅用于本次测试评估，不参与应用运行。
使用独立数据库 docs/dev/_test.db，绝不触碰 data/campusflow.db。
输出：docs/dev/_closed_loop.md
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import planner_agent, reminder_agent, report_agent  # noqa: E402
from database import sqlite_db  # noqa: E402

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "_results"
TEST_DB = BASE / "_test.db"
OUT_MD = BASE / "_closed_loop.md"

TODAY = date(2026, 10, 6)


def main() -> None:
    if TEST_DB.exists():
        TEST_DB.unlink()
    sqlite_db.init_db(TEST_DB)

    # ---------- 1. 汇总全部识别结果 ----------
    raw_tasks: list[dict] = []
    source_stat: list[tuple[str, int]] = []
    for path in sorted(RESULT_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        tasks = payload.get("tasks") or []
        raw_tasks.extend(tasks)
        source_stat.append((payload.get("name", "?"), len(tasks)))
    print(f"识别环节共产出 {len(raw_tasks)} 条事项")

    # ---------- 2. PlannerAgent：优先级补齐 + 风险分级 ----------
    planned = planner_agent.plan(raw_tasks, today=TODAY)
    priority_none_before = sum(1 for t in raw_tasks if not t.get("priority"))
    priority_none_after = sum(1 for t in planned if not t.get("priority"))
    risk_stat: dict[str, int] = {}
    for item in planned:
        key = item.get("risk_level") or "unknown"
        risk_stat[key] = risk_stat.get(key, 0) + 1
    print(f"优先级补齐：{priority_none_before} 条缺失 → 补齐后仍缺失 {priority_none_after} 条")
    print(f"风险分级：{risk_stat}")

    # ---------- 3. 入库 ----------
    created = sqlite_db.batch_create(planned, db_path=TEST_DB)
    print(f"成功入库 {len(created)} 条（含 create 日志）")

    # ---------- 4. 状态流转：模拟真实使用 ----------
    changed = {"processing": 0, "completed": 0}
    for item in created:
        deadline = item.get("deadline")
        if not deadline:
            continue
        days_left = (date.fromisoformat(deadline) - TODAY).days
        task_id = item["id"]
        if days_left < 0:
            continue  # 已过期，保持待办，由 ReminderAgent 判定逾期
        if days_left <= 7:
            sqlite_db.update_status(task_id, "processing", db_path=TEST_DB)
            changed["processing"] += 1
        elif days_left <= 30:
            sqlite_db.update_status(
                task_id, "completed", db_path=TEST_DB, remark="测试模拟已完成"
            )
            changed["completed"] += 1
    print(f"状态流转：进行中 {changed['processing']} 条，已完成 {changed['completed']} 条")

    # ---------- 5. ReminderAgent：提醒分级 ----------
    reminders = reminder_agent.check_all(today=TODAY, db_path=TEST_DB)
    level_stat: dict[str, int] = {}
    for item in reminders:
        key = item.get("level", "?")
        level_stat[key] = level_stat.get(key, 0) + 1
    print(f"提醒生成：共 {len(reminders)} 条 {level_stat}")

    # ---------- 6. ReportAgent：驾驶舱 + 周报 ----------
    dashboard = report_agent.generate_dashboard(today=TODAY, db_path=TEST_DB)
    weekly = report_agent.generate_weekly_report(today=TODAY, db_path=TEST_DB)
    print(f"驾驶舱：{dashboard}")

    # ---------- 7. 落盘报告 ----------
    lines: list[str] = [
        "# CampusFlow AI 闭环链路验证报告",
        "",
        f"- 基准日期：{TODAY.isoformat()}",
        f"- 测试数据库：`{TEST_DB.name}`（独立库，未使用 data/campusflow.db）",
        f"- 事项总数：{len(created)}",
        "",
        "## 一、各材料识别条数",
        "",
        "| 材料 | 事项数 |",
        "| --- | --- |",
    ]
    for name, count in source_stat:
        lines.append(f"| {name.split('__')[-1][:50]} | {count} |")

    lines += [
        "",
        "## 二、规划环节",
        "",
        f"- 优先级缺失：识别阶段 {priority_none_before} 条 → 规划后 {priority_none_after} 条",
        f"- 风险分级分布：{risk_stat}",
        "",
        "## 三、状态流转（模拟）",
        "",
        f"- 进行中：{changed['processing']} 条",
        f"- 已完成：{changed['completed']} 条",
        "",
        "## 四、提醒分级（ReminderAgent）",
        "",
        f"- 提醒总数：{len(reminders)}",
        f"- 等级分布：{level_stat}",
        "",
        "## 五、驾驶舱统计（ReportAgent）",
        "",
        "```json",
        json.dumps(dashboard, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 六、闭环执行周报（ReportAgent 自动生成）",
        "",
        weekly,
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n闭环报告已写入：{OUT_MD}")


if __name__ == "__main__":
    main()