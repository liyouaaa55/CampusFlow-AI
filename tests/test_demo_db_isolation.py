"""演示数据环境隔离测试（docs/dev/demo_app.py + docs/dev/demo_db.py）。

覆盖目标（全部为**运行行为断言**）：
    1. 路径切换把数据库 / 上传 / 附件四个落点全部指向演示目录；
    2. 演示库按既有建表逻辑初始化为**空库**，表结构与项目一致；
    3. 写入演示库（任务 / 日志 / 反馈 / 核验）**不改变开发库**的任何计数；
    4. 快照保存 → 恢复可回到基线状态，且 reset 前自动备份；
    5. 基线缺失时 reset **明确失败**，不代替"清空数据库"；
    6. 附件清理只删除"数据库已无引用"的文件，不误删已引用文件；
    7. 路径守卫拒绝开发库 / 开发附件目录 / 演示目录之外的任何目标；
    8. status 为纯只读，不会创建数据库；
    9. 附件落盘确实进入演示目录（复用真实 save_attachment）。

所有写入都发生在 tmp_path 构造的演示根目录内；开发库只以只读方式做前后对比。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEV_SCRIPTS = PROJECT_ROOT / "docs" / "dev"
if str(DEV_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(DEV_SCRIPTS))

import demo_db  # noqa: E402
from config import settings  # noqa: E402
from database import sqlite_db, verification_store  # noqa: E402
from tools import execution_check, task_dispatch, verification_service  # noqa: E402
from ui.pages import submission_view  # noqa: E402


# ============================================================
# 夹具
# ============================================================


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把演示根目录指向 tmp_path，并保证被修改的全局常量在用例后自动还原。"""
    for obj, name in (
        (settings, "DATA_DIR"),
        (settings, "UPLOAD_DIR"),
        (settings, "DB_PATH"),
        (sqlite_db, "DB_PATH"),
        (submission_view, "SUBMISSION_DIR"),
    ):
        monkeypatch.setattr(obj, name, getattr(obj, name))
    return demo_db.demo_paths(tmp_path / "demo")


def _dev_counts() -> dict[str, int]:
    """只读统计开发库关键表行数（不创建、不修改）。"""
    if not demo_db.DEV_DB_PATH.exists():
        return {}
    import sqlite3

    connection = sqlite3.connect(
        f"file:{demo_db.DEV_DB_PATH.as_posix()}?mode=ro", uri=True, timeout=10.0
    )
    try:
        return {
            table: int(connection.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("tasks", "task_logs", "task_feedback", "verifications")
        }
    finally:
        connection.close()


def _create_task(db_path: Path, title: str = "演示任务") -> int:
    return int(
        sqlite_db.create_task(
            {
                "title": title,
                "department": "保卫处",
                "owner": "王老师",
                "deadline": "2099-12-31",
                "priority": "medium",
                "risk_level": "green",
                "need_confirm": False,
                "description": "演示环境专用任务",
            },
            db_path=db_path,
        )["id"]
    )


class _FakeUpload:
    """最小可用的上传对象替身（save_attachment 只用 name / size / getbuffer）。"""

    def __init__(self, name: str, payload: bytes = b"demo") -> None:
        self.name = name
        self.size = len(payload)
        self._payload = payload

    def getbuffer(self) -> bytes:
        return self._payload


# ============================================================
# 一、路径切换与空库初始化
# ============================================================


def test_activate_redirects_every_landing_point(sandbox) -> None:
    """切换后：数据库 / 上传 / 附件 / DATA_DIR 全部落在演示目录内。"""
    demo_db.activate_demo_paths(sandbox.root)

    assert settings.DB_PATH == sandbox.db
    assert sqlite_db.DB_PATH == sandbox.db
    assert settings.UPLOAD_DIR == sandbox.uploads
    assert settings.DATA_DIR == sandbox.root
    # SUBMISSION_DIR 是导入时求值的模块常量，必须被显式改写，否则附件会写回开发目录
    assert submission_view.SUBMISSION_DIR == sandbox.submissions

    for directory in (sandbox.root, sandbox.uploads, sandbox.submissions):
        assert directory.is_dir()


def test_demo_app_switches_paths_before_importing_app() -> None:
    """锁定 demo_app.py 的关键顺序：先切路径，再导入 app（否则附件会写回开发目录）。

    断言用行首换行锚定，避免命中文档字符串里的同名示例。
    """
    source = (DEV_SCRIPTS / "demo_app.py").read_text(encoding="utf-8")
    switch_at = source.index("\nPATHS = activate_demo_paths()")
    import_at = source.index("\nfrom app import main")
    assert switch_at < import_at, "必须先切换演示路径，再导入应用"


def test_init_creates_empty_demo_db_with_current_schema(sandbox) -> None:
    """首次初始化：按项目既有建表逻辑建空库，不含任何任务。"""
    demo_db.init_demo_db(sandbox)

    assert sandbox.db.exists()
    counts = demo_db.table_counts(sandbox.db)
    assert set(counts) == set(demo_db.MAIN_TABLES)
    assert all(value == 0 for value in counts.values()), "演示库必须是空库"
    assert sandbox.db != demo_db.DEV_DB_PATH


def test_status_is_read_only_and_does_not_create_database(sandbox, capsys) -> None:
    """status 不得创建数据库。"""
    assert demo_db.cmd_status(sandbox) == 0
    assert not sandbox.db.exists(), "status 必须是只读，不能创建演示库"
    assert "演示库尚未创建" in capsys.readouterr().out


# ============================================================
# 二、演示写入不污染开发库
# ============================================================


def test_writing_demo_data_does_not_change_dev_db(sandbox) -> None:
    """在演示库跑完整业务写入后，开发库四张关键表计数完全不变。"""
    before = _dev_counts()
    demo_db.activate_demo_paths(sandbox.root)
    demo_db.init_demo_db(sandbox)

    task_id = _create_task(sandbox.db, "演示任务A")            # tasks + task_logs
    task_dispatch.dispatch_task(task_id, db_path=sandbox.db)   # task_logs
    execution_check.save_feedback(                            # task_feedback
        task_id, status="in_progress", note="演示反馈", db_path=sandbox.db
    )
    task_dispatch.submit_work(task_id, "演示办理结果", db_path=sandbox.db)
    verification_service.verify_submission(                    # verifications
        task_id, decision="approved", db_path=sandbox.db
    )

    after = _dev_counts()
    assert after == before, "演示写入不得改变开发库计数"
    # 演示库自身确实写进去了（否则本用例毫无意义）
    assert int(sqlite_db.get_task(task_id, db_path=sandbox.db)["id"]) == task_id
    assert len(verification_store.list_verifications(task_id, db_path=sandbox.db)) == 1


def test_attachment_is_saved_into_demo_submissions_dir(sandbox) -> None:
    """复用真实 save_attachment：附件必须落在演示目录，且不进入开发附件目录。"""
    demo_db.activate_demo_paths(sandbox.root)

    saved = submission_view.save_attachment(_FakeUpload("现场照片.png"))

    assert saved is not None
    assert sandbox.submissions in saved.parents
    assert demo_db.DEV_SUBMISSIONS not in saved.parents
    assert saved.is_file()


# ============================================================
# 三、快照备份与恢复
# ============================================================


def test_backup_then_reset_restores_baseline(sandbox) -> None:
    """backup 保存基线 → reset 回到基线状态，并自动备份 reset 前的演示库。"""
    demo_db.init_demo_db(sandbox)
    first = _create_task(sandbox.db, "基线任务1")
    second = _create_task(sandbox.db, "基线任务2")

    assert demo_db.cmd_backup(sandbox) == 0
    assert sandbox.baseline.exists()

    extra = _create_task(sandbox.db, "基线之后新增的任务")
    assert extra in {int(task["id"]) for task in sqlite_db.list_tasks(db_path=sandbox.db)}

    assert demo_db.cmd_reset(sandbox) == 0

    titles = {task["title"] for task in sqlite_db.list_tasks(db_path=sandbox.db)}
    assert titles == {"基线任务1", "基线任务2"}, "恢复后应与基线完全一致"
    assert sqlite_db.get_task(extra, db_path=sandbox.db) is None
    assert sqlite_db.get_task(first, db_path=sandbox.db) is not None
    assert sqlite_db.get_task(second, db_path=sandbox.db) is not None

    backups = list(sandbox.backup_dir.glob("pre-reset-*.db"))
    assert len(backups) == 1, "reset 前必须自动备份当前演示库"


def test_reset_without_baseline_fails_and_keeps_data(sandbox) -> None:
    """基线不存在时 reset 必须明确失败，且不得清空数据库代替恢复。"""
    demo_db.init_demo_db(sandbox)
    _create_task(sandbox.db, "不能丢的任务")

    with pytest.raises(demo_db.DemoError) as excinfo:
        demo_db.cmd_reset(sandbox)

    assert "基线快照不存在" in str(excinfo.value)
    assert "backup" in str(excinfo.value), "失败提示必须告诉用户如何创建基线"
    assert not sandbox.baseline.exists()
    assert len(sqlite_db.list_tasks(db_path=sandbox.db)) == 1, "失败时不得改动数据库"


def test_backup_refuses_to_overwrite_without_force(sandbox) -> None:
    """基线已存在时默认拒绝覆盖，避免误删已验收的基线。"""
    demo_db.init_demo_db(sandbox)
    _create_task(sandbox.db)
    demo_db.cmd_backup(sandbox)
    size = sandbox.baseline.stat().st_size

    with pytest.raises(demo_db.DemoError):
        demo_db.cmd_backup(sandbox)
    assert sandbox.baseline.stat().st_size == size

    assert demo_db.cmd_backup(sandbox, force=True) == 0


def test_reset_removes_only_unreferenced_attachments(sandbox) -> None:
    """附件同步：只删数据库中已无引用的文件，已引用文件必须保留。"""
    demo_db.init_demo_db(sandbox)
    task_id = _create_task(sandbox.db, "带附件的任务")
    task_dispatch.dispatch_task(task_id, db_path=sandbox.db)

    kept_file = sandbox.submissions / "20260101_台账.xlsx"
    kept_file.write_bytes(b"baseline")
    task_dispatch.submit_work(
        task_id, "基线办理结果", file_path=str(kept_file), db_path=sandbox.db
    )
    demo_db.cmd_backup(sandbox)

    # 基线之后：新增一个被引用的附件 + 一个无人引用的孤儿文件 + 上传目录的孤儿文件
    later_file = sandbox.submissions / "20260202_补充材料.xlsx"
    later_file.write_bytes(b"later")
    execution_check.save_feedback(
        task_id,
        status="in_progress",
        note="补充材料",
        file_path=str(later_file),
        db_path=sandbox.db,
    )
    orphan = sandbox.submissions / "20260303_孤儿附件.xlsx"
    orphan.write_bytes(b"orphan")
    orphan_upload = sandbox.uploads / "20260303_孤儿上传.txt"
    orphan_upload.write_bytes(b"orphan-upload")

    assert demo_db.cmd_reset(sandbox) == 0

    assert kept_file.is_file(), "基线引用的附件必须保留"
    assert not orphan.exists(), "无引用附件必须删除"
    assert not orphan_upload.exists(), "演示上传目录中的无引用文件必须清理"
    assert not later_file.exists(), "基线之后新增的附件应被清理"


# ============================================================
# 四、路径守卫
# ============================================================


def test_path_guard_rejects_dev_resources() -> None:
    """开发库、开发附件目录、data 目录本身都不是合法目标。"""
    for forbidden in (
        demo_db.DEV_DB_PATH,
        demo_db.DEV_UPLOADS,
        demo_db.DEV_SUBMISSIONS,
        demo_db.DATA_DIR,
    ):
        with pytest.raises(demo_db.DemoError):
            demo_db.assert_demo_path(forbidden)


def test_path_guard_rejects_paths_outside_demo_root(tmp_path: Path) -> None:
    """演示目录之外的任何文件（含项目源码）都必须被拒绝。"""
    root = demo_db.demo_paths(tmp_path / "demo")
    with pytest.raises(demo_db.DemoError):
        demo_db.assert_demo_path(DEV_SCRIPTS / "demo_db.py", root=root)
    with pytest.raises(demo_db.DemoError):
        demo_db.assert_demo_path(root.root, root=root)
    # 演示目录之内的目标合法
    assert demo_db.assert_demo_path(root.db, root=root) == root.db


def test_safe_backup_refuses_dev_db_as_target(tmp_path: Path) -> None:
    """备份目标指向开发库时必须中止。"""
    with pytest.raises(demo_db.DemoError):
        demo_db.safe_backup(tmp_path / "x.db", demo_db.DEV_DB_PATH, root=tmp_path / "demo")


def test_cli_exposes_expected_commands() -> None:
    """CLI 至少提供 status / init / backup / reset。"""
    parser = demo_db.build_parser()
    for command in ("status", "init", "backup", "reset"):
        args = parser.parse_args([command])
        assert args.command == command