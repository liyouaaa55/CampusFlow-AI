"""演示数据准备脚本（只作用于 8502 演示环境）。

用法::

    python docs/dev/demo_seed.py --dry-run     # 只打印将要生成的数据，不写库
    python docs/dev/demo_seed.py               # 生成演示数据 + 完整性核验 + 输出清单
    python docs/dev/demo_seed.py --verify-only # 只做只读核验并重新输出清单

三条纪律
--------
1. **只写演示库**：进入业务模块之前先调用 demo_db.activate_demo_paths()，
   把数据库 / 上传 / 附件四个落点全部切到 data/demo/；开发库不在任何写路径上。
2. **只用现有业务接口**：任务、确认、下发、回执、反馈、核验、闭环全部通过
   database / tools 层的既有函数写入，不直接拼 SQL、不手工改字段。
3. **不重复生成**：演示库中已存在任务时直接中止。

数据为虚构演示数据：单位、部门、人名、材料内容均为虚构。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from demo_db import activate_demo_paths, demo_paths  # noqa: E402

PATHS = activate_demo_paths()

from database import (  # noqa: E402
    closure_store,
    feedback_store,
    models,
    sqlite_db,
    submission_store,
    task_metadata_store,
    verification_store,
)
from tools import closure_service, execution_check, task_dispatch, verification_service  # noqa: E402
from ui.pages import submission_view  # noqa: E402

TODAY = date.today()
CHECKLIST = HERE / "demo_data_checklist.md"
ORG = "示范大学"
TABLES = ("tasks", "task_logs", "task_feedback", "task_submissions",
          "verifications", "task_closures", "task_metadata")


@dataclass
class DemoTask:
    """一条演示任务的意图；实际写入全部交给业务接口。"""

    key: str
    title: str
    department: str
    owner: str
    deadline_days: int
    status: str
    priority: str
    risk_level: str
    need_confirm: bool = False
    description: str = ""
    confirm: bool = False
    dispatch: bool = False
    criteria: list = field(default_factory=list)
    submit: str | None = None
    submit_file: tuple | None = None
    verify: str | None = None
    verify_reason: str = ""
    feedback: tuple | None = None
    feedback_file: tuple | None = None
    close: str | None = None
    note: str = ""


DEMO_TASKS = (
    DemoTask(
        key="A1", title="校运会田径场地布置与器材摆放", department="体育部", owner="王建国",
        deadline_days=14, status="pending", priority="high", risk_level="green", need_confirm=True,
        description=f"{ORG}校运会筹备：田径场地分块绘制、器材定点摆放与赛前安全检查（演示用虚构事项）。",
        note="演示第三页「任务确认 → 下发」起点：待人工确认，不可直接下发。",
    ),
    DemoTask(
        key="A2", title="开幕式方阵编排与主持词撰写", department="宣传部", owner="李文彬",
        deadline_days=7, status="confirmed", priority="medium", risk_level="green", dispatch=True,
        description=f"{ORG}校运会开幕式方阵编排方案与主持词初稿（演示用虚构事项）。",
        note="已确认未反馈：演示「已下发未反馈」督办判定。",
    ),
    DemoTask(
        key="A3", title="裁判员选聘与竞赛规则培训", department="教务处", owner="张慧",
        deadline_days=2, status="urgent", priority="high", risk_level="yellow", dispatch=True,
        description=f"{ORG}校运会裁判员选聘名单确认与赛前规则培训安排（演示用虚构事项）。",
        note="临期（urgent）：演示「临期未完成」督办判定与提醒等级。",
    ),
    DemoTask(
        key="B1", title="志愿者招募与通用培训", department="校团委", owner="陈晓",
        deadline_days=5, status="processing", priority="medium", risk_level="green", dispatch=True,
        description=f"{ORG}校运会志愿者招募名额、岗位安排与通用培训（演示用虚构事项）。",
        feedback=("in_progress", "已完成志愿者招募渠道对接，岗位培训排期待教务处确认后开班。"),
        feedback_file=("志愿者培训台账.csv", "序号,培训场次,参训人数,备注\n1,第一场,120,演示数据\n2,第二场,待定,演示数据\n"),
        note="已有进行中反馈与附件：演示第四页反馈摘要与附件展示。",
    ),
    DemoTask(
        key="B2", title="赛时医疗点设置与急救药品配备", department="后勤处", owner="刘洋",
        deadline_days=1, status="urgent", priority="high", risk_level="red", dispatch=True,
        description=f"{ORG}校运会赛时医疗点选址、急救药品与担架配备（演示用虚构事项）。",
        submit="已提交医疗点设置方案与急救药品清单（演示用虚构内容）。",
        submit_file=("医疗点物资清单.md", "# 医疗点物资清单（演示数据 虚构）\n\n- 医疗点 3 处\n- 急救药品：待补充\n- 担架 6 台\n"),
        verify="need_supplement",
        verify_reason="急救药品清单不完整，需补充后重新提交（演示：核验未通过）。",
        note="核验未通过：演示「需补充材料」与闭环被阻断的真实原因。",
    ),
    DemoTask(
        key="B3", title="校园宣传栏与电子屏内容审核发布", department="宣传部", owner="李文彬",
        deadline_days=-5, status="overdue", priority="high", risk_level="yellow", dispatch=True,
        description=f"{ORG}校运会校园宣传栏与电子屏内容审核、发布与撤除安排（演示用虚构事项）。",
        note="已逾期未反馈：演示「已逾期未完成」督办判定与风险颜色。",
    ),
    DemoTask(
        key="C1", title="奖牌奖品与应急物资采购验收入库", department="资产处", owner="赵敏",
        deadline_days=9, status="processing", priority="high", risk_level="green", dispatch=True,
        description=f"{ORG}校运会奖牌奖品与应急物资采购、验收与入库（演示用虚构事项）。",
        criteria=["提交采购验收单", "物资入库登记完成"],
        submit="奖牌奖品与应急物资已到货并完成验收登记（演示用虚构内容）。",
        submit_file=("采购验收单.csv", "物资名称,数量,验收结论\n奖牌,600,演示数据-合格\n应急物资包,80,演示数据-合格\n"),
        verify="approved",
        verify_reason="验收单齐备、物资已入库，符合办结标准（演示用虚构结论）。",
        note="核验通过、满足闭环条件但尚未闭环：留作现场演示正式闭环。",
    ),
    DemoTask(
        key="C2", title="场馆消防与用电安全检查整改", department="后勤处", owner="刘洋",
        deadline_days=3, status="processing", priority="high", risk_level="yellow", dispatch=True,
        description=f"{ORG}校运会场馆消防通道、用电线路与配电箱安全检查及整改（演示用虚构事项）。",
        feedback=("in_progress", "三个场馆配电箱已完成逐一检查，两处隐患已整改，一处待更换。"),
        feedback_file=("消防整改记录.md", "# 场馆消防与用电整改记录（演示数据 虚构）\n\n- 一号馆：整改完成\n- 二号馆：整改完成\n- 三号馆：待更换配电箱\n"),
        note="临期 + 已有进展：演示督办依据中的「最新反馈」。",
    ),
    DemoTask(
        key="D1", title="校运会秩序册编印与发放", department="教务处", owner="张慧",
        deadline_days=-2, status="completed", priority="medium", risk_level="green", dispatch=True,
        description=f"{ORG}校运会秩序册编印与各代表队发放（演示用虚构事项）。",
        criteria=["提交秩序册发放记录", "各代表队签收完成"],
        submit="秩序册已编印完成并按名册发放（演示用虚构内容）。",
        submit_file=("秩序册发放记录.csv", "代表队,份数,签收状态\n第一代表团,120,演示数据-已签收\n第二代表团,110,演示数据-已签收\n"),
        verify="approved",
        verify_reason="发放记录齐全，符合办结标准（演示用虚构结论）。",
        close="秩序册编印与发放已完成并核验通过，予以正式闭环（演示用虚构结论）。",
        note="完整闭环链路：下发 → 回执 → 核验 → 闭环（报告口径中的逾期后完成）。",
    ),
    DemoTask(
        key="D2", title="校园广播与计时系统联调", department="信息化中心", owner="吴强",
        deadline_days=-3, status="completed", priority="medium", risk_level="green", dispatch=True,
        description=f"{ORG}校运会校园广播与计时系统的联调与压力测试（演示用虚构事项）。",
        criteria=["提交系统联调记录"],
        submit="广播与计时系统联调完成，各点位测试通过（演示用虚构内容）。",
        verify="approved",
        verify_reason="联调记录已提交，符合办结标准（演示用虚构结论）。",
        close="系统联调完成并核验通过，予以正式闭环（演示用虚构结论）。",
        note="第二条已闭环任务：让闭环数量看起来自然。",
    ),
    DemoTask(
        key="E1", title="观众入场安检动线设计与标识制作", department="保卫处", owner="郑凯",
        deadline_days=20, status="pending", priority="medium", risk_level="green", need_confirm=True,
        description=f"{ORG}校运会观众入场安检动线设计、标识制作与志愿者引导安排（演示用虚构事项）。",
        note="待确认未下发：演示「人工确认后才能下发」的门禁。",
    ),
    DemoTask(
        key="E2", title="赛后场地清理与设备归还", department="体育部", owner="王建国",
        deadline_days=16, status="pending", priority="low", risk_level="green", need_confirm=True,
        description=f"{ORG}校运会赛后场地清理、器材回收与租借设备归还（演示用虚构事项）。",
        note="低优先级待办：让优先级分布更自然。",
    ),
    DemoTask(
        key="F1", title="（待人工确认）志愿服务时长认定方式研究", department="教务处", owner="张慧",
        deadline_days=18, status="pending", priority="medium", risk_level="yellow", need_confirm=True,
        description=f"{ORG}相关材料仅提出志愿服务时长认定方式需研究，未形成明确执行事项；本条用于演示材料未给出明确任务时必须人工确认、不凭空生成任务的机制。",
        note="体现无明确任务材料需人工确认：不下发、不生成反馈。",
    ),
)


class _File:
    """最小上传对象替身：复用真实的 submission_view.save_attachment 落盘逻辑。"""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        payload = text.encode("utf-8")
        self.size = len(payload)
        self._payload = payload

    def getbuffer(self) -> bytes:
        return self._payload


def _write_attachment(spec) -> str | None:
    """把虚构附件写入演示附件目录（由 demo_db.activate_demo_paths 已切换）。"""
    saved = submission_view.save_attachment(_File(spec[0], spec[1]))
    return str(saved) if saved else None


def _payload_for(task: DemoTask) -> dict:
    deadline = (TODAY + timedelta(days=task.deadline_days)).isoformat()
    return {
        "title": task.title,
        "department": task.department,
        "owner": task.owner,
        "deadline": deadline,
        "priority": task.priority,
        "status": task.status,
        "risk_level": task.risk_level,
        "need_confirm": task.need_confirm,
        "description": task.description or None,
    }


def prepare(dry_run: bool = False) -> dict:
    """按依赖顺序写入演示数据，返回 {key: task_id}。"""
    if dry_run:
        print("[dry-run] 不会写库。将生成以下演示任务：")
        for task in DEMO_TASKS:
            deadline = (TODAY + timedelta(days=task.deadline_days)).isoformat()
            print(f"  - {task.key:<3} {task.title} | {task.department} | {task.owner}"
                  f" | {task.status} | 截止 {deadline}")
        return {}

    created = sqlite_db.batch_create([_payload_for(task) for task in DEMO_TASKS])
    ids = {task.key: int(record["id"]) for task, record in zip(DEMO_TASKS, created)}
    print(f"已创建 {len(ids)} 条演示任务：{ids}")

    for task in DEMO_TASKS:
        task_id = ids[task.key]
        if task.confirm:
            sqlite_db.update_status(task_id, "confirmed", operator="李处长",
                                    remark="人工确认任务（下发前置条件）", db_path=PATHS.db)
        if task.dispatch:
            task_dispatch.dispatch_task(task_id, db_path=PATHS.db)
        if task.criteria:
            task_metadata_store.set_closure_criteria(
                task_id, task.criteria, closure_source=models.CLOSURE_SOURCE_MATERIAL,
                closure_confirmed=True, db_path=PATHS.db)
        if task.submit:
            file_path = _write_attachment(task.submit_file) if task.submit_file else None
            task_dispatch.submit_work(task_id, task.submit, file_path=file_path, db_path=PATHS.db)
        if task.verify:
            verification_service.verify_submission(
                task_id, decision=task.verify, reason=task.verify_reason or None,
                operator="李主任", db_path=PATHS.db)
        if task.feedback:
            file_path = _write_attachment(task.feedback_file) if task.feedback_file else None
            outcome = execution_check.save_feedback(
                task_id, status=task.feedback[0], note=task.feedback[1],
                file_path=file_path, operator="李主任", db_path=PATHS.db)
            if not outcome.get("ok"):
                print(f"  [反馈失败] {task.key}：{outcome.get('reason')}")
        if task.close:
            closure = closure_service.close_task(
                task_id, closed_by="李主任", closure_note=task.close, db_path=PATHS.db)
            if not closure.get("ok"):
                print(f"  [闭环失败] {task.key}：{closure.get('reason')}")
        print(f"  - {task.key} #{task_id} {task.title}")
    return ids


def _readonly():
    return sqlite3.connect(f"file:{PATHS.db.as_posix()}?mode=ro", uri=True, timeout=10.0)


def verify() -> dict:
    """只读核验：路径、表计数、外键、孤立记录、附件一致性、统计口径。"""
    print("=" * 74)
    print("演示数据完整性核验（只读）")
    print("=" * 74)
    print(f"演示数据库  : {PATHS.db}")
    print(f"上传目录    : {PATHS.uploads}")
    print(f"附件目录    : {PATHS.submissions}")
    print(f"开发库      : {demo_paths().root.parent / 'campusflow.db'}（本脚本不写）")
    con = _readonly()
    try:
        counts = {t: int(con.execute(f"select count(*) from {t}").fetchone()[0]) for t in TABLES}
        print("-" * 74)
        print("表计数:", counts)
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        print(f"外键检查: {'通过（无违规）' if not fk else f'{len(fk)} 条违规 {fk}'}")
        orphans = {}
        for table in TABLES[1:]:
            orphans[table] = int(con.execute(
                f"select count(*) from {table} t where not exists "
                f"(select 1 from tasks x where x.id = t.task_id)").fetchone()[0])
        no_create = int(con.execute(
            "select count(*) from tasks t where not exists "
            "(select 1 from task_logs l where l.task_id = t.id and l.action = 'create')").fetchone()[0])
        print("孤立记录（无对应任务）:", orphans)
        print(f"缺少 create 日志的任务数: {no_create}")
        print("状态分布:", con.execute(
            "select status, count(*) from tasks group by status order by count(*) desc").fetchall())
        print("优先级分布:", con.execute("select priority, count(*) from tasks group by priority").fetchall())
        print("风险分布:", con.execute("select risk_level, count(*) from tasks group by risk_level").fetchall())

        referenced = set()
        for table in ("task_feedback", "task_submissions"):
            for (value,) in con.execute(f"select file_path from {table}"):
                if value:
                    referenced.add(Path(str(value)).name)
        actual = {i.name for d in (PATHS.uploads, PATHS.submissions) if d.is_dir()
                  for i in d.iterdir() if i.is_file()}
        missing = sorted(referenced - actual)
        extra = sorted(actual - referenced)
        print(f"附件引用 {len(referenced)} 个 / 实际文件 {len(actual)} 个")
        print(f"引用但文件缺失: {missing or '无'}")
        print(f"文件未被引用  : {extra or '无'}")
        # 数据库只保存**文件名**（不含绝对路径），因此按文件名核对实际落盘位置，
        # 确认每个被引用的附件都真实存在于演示附件目录内。
        outside = []
        for name in sorted(actual):
            if name not in referenced:
                continue
            where = [d for d in (PATHS.uploads, PATHS.submissions) if (d / name).exists()]
            if not where or PATHS.root.resolve() not in where[0].resolve().parents:
                outside.append(name)
        print(f"附件落盘位置异常: {outside or '无（全部位于演示目录内）'}")
        print("数据库中 file_path 的存储形态示例:", sorted(referenced)[:1])
    finally:
        con.close()

    print("-" * 74)
    rows = execution_check.build_task_rows(today=TODAY, db_path=PATHS.db)
    print("统一统计口径（第五页报告同源）:", execution_check.build_overview(rows))
    supervision = execution_check.supervision_items(today=TODAY, rows=rows)
    print(f"持续督办清单（第四页同源）: {len(supervision)} 项")
    for item in supervision:
        print(f"  - [{item['tag']}] {item['title']}")
    return {"counts": counts, "fk": fk, "orphans": orphans, "no_create_log": no_create,
            "supervision": len(supervision), "missing_files": missing,
            "extra_files": extra, "outside": outside}


def _task_id_of(task: DemoTask) -> int:
    """按标题定位任务 ID（演示数据内标题唯一）。"""
    for row in sqlite_db.list_tasks(db_path=PATHS.db):
        if row["title"] == task.title:
            return int(row["id"])
    return -1


def write_checklist() -> Path:
    """生成 Markdown 数据清单（供负责人核对，不入库）。"""
    lines = [
        "# 校运会演示数据清单（8502 演示环境）",
        "",
        f"> 生成时间：{TODAY.isoformat()}　|　数据来源：`docs/dev/demo_seed.py`",
        f"> 演示数据库：`{PATHS.db}`",
        "> 全部为虚构演示数据：单位、部门、人员、材料内容均非真实信息。",
        "",
        "## 任务与关联记录",
        "",
        "| # | 标题 | 部门 | 负责人 | 状态 | 优先级 | 风险 | 截止时间 | 回执 | 反馈 | 附件 | 核验 | 闭环 | 演示要点 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for task in DEMO_TASKS:
        task_id = _task_id_of(task)
        if task_id < 0:
            continue
        row = sqlite_db.get_task(task_id, db_path=PATHS.db) or {}
        if not row:
            continue
        feedback = feedback_store.list_feedback(task_id, db_path=PATHS.db)
        submission = submission_store.get_submission(task_id, db_path=PATHS.db)
        verifications = verification_store.list_verifications(task_id, db_path=PATHS.db)
        closure = closure_store.get_closure(task_id, db_path=PATHS.db)
        attachment = ""
        for record in list(feedback) + ([submission] if submission else []):
            if record and record.get("file_path"):
                attachment = Path(str(record["file_path"])).name
                break
        lines.append(
            f"| {task_id} | {task.title} | {task.department} | {task.owner} | {row['status']} | "
            f"{row.get('priority') or '-'} | {row.get('risk_level') or '-'} | {row.get('deadline') or '-'} | "
            f"{'有' if submission else '—'} | "
            f"{str(len(feedback)) + ' 条（' + feedback[-1]['status'] + '）' if feedback else '—'} | "
            f"{attachment or '—'} | {verifications[-1]['status'] if verifications else '—'} | "
            f"{'已闭环' if closure else '—'} | {task.note} |")
    lines += ["", "## 状态分布", ""]
    con = _readonly()
    try:
        for status, count in con.execute(
                "select status, count(*) from tasks group by status order by count(*) desc"):
            lines.append(f"- `{status}`：{count} 条")
    finally:
        con.close()
    lines += ["", "## 演示附件目录", "", f"`{PATHS.submissions}`", ""]
    for item in sorted(PATHS.submissions.iterdir()):
        if item.is_file():
            lines.append(f"- `{item.name}`（{item.stat().st_size} 字节）")
    CHECKLIST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"清单已写入：{CHECKLIST}")
    return CHECKLIST


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="准备 8502 演示环境的比赛演示数据")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要生成的数据，不写库")
    parser.add_argument("--verify-only", action="store_true", help="只做只读核验并重新输出清单")
    args = parser.parse_args(argv)

    sqlite_db.init_db(PATHS.db)

    if args.verify_only:
        verify()
        write_checklist()
        return 0

    existing = sqlite_db.list_tasks(db_path=PATHS.db)
    if existing:
        print(f"[中止] 演示库已存在 {len(existing)} 条任务，拒绝重复生成。\n"
              "如需重新准备：先执行 `python docs/dev/demo_db.py reset`（需已有基线）。",
              file=sys.stderr)
        return 2

    prepare(dry_run=args.dry_run)
    if args.dry_run:
        return 0
    verify()
    write_checklist()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
