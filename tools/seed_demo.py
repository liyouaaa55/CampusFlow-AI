"""演示数据种子脚本（Synthetic Data）。

用途：一键把一批**虚构的**高校事务写入 SQLite Memory，方便在没有真实数据时
预览 Dashboard / 任务中心 / 自动报告三个页面的效果。

重要声明：
- 本脚本产生的全部内容均为「演示数据（Synthetic Data）」，不代表任何真实单位；
- 数据覆盖 4 个部门、6 种状态、4 种提醒等级，用于演示完整的闭环视图；
- 运行方式：python -m tools.seed_demo
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from agent import planner_agent
from config import settings
from database import sqlite_db

# (标题, 部门, 负责人, 相对截止天数, 优先级, 状态, 事项说明)
# 相对天数：负数=已逾期、0~1=紧急、2~3=临期、更大=正常；None 表示不设截止时间
DEMO_TASKS: tuple[tuple[Any, ...], ...] = (
    # ---------- 学工办 ----------
    ("举办智能机器人创新周", "学工办", "张老师", 12, "medium", "processing", "制定活动实施方案并完成物资采购"),
    ("整理新生入学教育总结材料", "学工办", "张老师", -3, "medium", "pending", "汇总各学院材料后统一归档"),
    ("组织大学生志愿服务月活动", "学工办", "李老师", 3, "low", "confirmed", "完成志愿者招募与培训安排"),
    ("完成学生资助材料审核", "学工办", None, 8, "medium", "pending", "按资助类别逐项复核材料完整性"),
    ("举办校园歌手大赛", "学工办", "王老师", 20, "low", "completed", "决赛已完成并完成总结归档"),
    # ---------- 保卫处 ----------
    ("完成宿舍消防隐患专项排查", "保卫处", "王老师", -1, "high", "processing", "建立整改台账并完成隐患销号"),
    ("组织秋季消防疏散演练", "保卫处", "李老师", 1, "high", "pending", "覆盖全部宿舍楼并留存演练记录"),
    ("巡检校园监控设备", "保卫处", "赵老师", 10, "medium", "pending", "逐栋排查摄像头在线状态"),
    ("完成门禁系统升级", "保卫处", "赵老师", 15, "medium", "completed", "已完成闸机改造并上线"),
    # ---------- 教务处 ----------
    ("完成下学期教材征订统计", "教务处", "陈老师", 2, "medium", "processing", "汇总各学院征订数据并报出版社"),
    ("组织期中教学检查", "教务处", "陈老师", 0, "high", "pending", "开展课堂巡查与教学材料抽查"),
    ("完成学籍信息核对", "教务处", "刘老师", -7, "medium", "completed", "已核对并修正异常学籍数据"),
    # ---------- 实验中心 ----------
    ("完成实验室安全准入培训", "实验中心", "孙老师", 1, "high", "pending", "覆盖全部新生并组织线上考核"),
    ("排查危化品存储隐患", "实验中心", "孙老师", -2, "high", "pending", "核对台账并整改通风与分类存放问题"),
    ("更新实验设备台账", "实验中心", "周老师", 10, "low", "completed", "已完成资产编号核对与贴标"),
    # 缺负责人 + 缺截止 + 缺优先级：用于演示 PlannerAgent 的 red 风险与人工确认
    ("制定实验室开放管理办法", "实验中心", None, None, None, "pending", "明确开放时段、预约流程与安全责任"),
)


def build_tasks(today: date | None = None) -> list[dict[str, Any]]:
    """构造演示任务（含相对截止时间换算），可直接交给 PlannerAgent。"""
    base = today or date.today()
    tasks: list[dict[str, Any]] = []

    for title, department, owner, offset, priority, status, description in DEMO_TASKS:
        tasks.append(
            {
                "title": title,
                "department": department,
                "owner": owner,
                "deadline": (base + timedelta(days=offset)).isoformat() if offset is not None else None,
                "priority": priority,
                "status": status,
                "description": description,
            }
        )
    return tasks


def seed(
    db_path: str | Path | None = None,
    today: date | None = None,
    reset: bool = True,
) -> list[dict[str, Any]]:
    """写入演示数据并返回入库结果。

    参数：
        db_path  目标数据库，默认 settings.DB_PATH
        today    基准日期，默认今天（便于测试固定日期）
        reset    True 时先清空既有任务，保证每次演示数据一致
    """
    target = Path(db_path) if db_path is not None else settings.DB_PATH

    if reset and target.exists():
        target.unlink()

    sqlite_db.init_db(target)

    # 先过一遍 PlannerAgent：补齐优先级并生成 risk_level / need_confirm
    planned = planner_agent.plan(build_tasks(today), today=today)
    return sqlite_db.batch_create(planned, db_path=target)


def main() -> int:
    """命令行入口：python -m tools.seed_demo"""
    created = seed()
    print(f"已写入演示数据（Synthetic Data）{len(created)} 条 → {settings.DB_PATH}")
    for task in created:
        print(f"  #{task['id']:<3} {task['status']:<10} {task['risk_level']:<6} {task['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
