"""演示环境数据管理 CLI：只读查看 / 快照备份 / 恢复（不触碰开发库）。

背景
----
`streamlit run app.py` 使用开发库 ``data/campusflow.db``。比赛演示需要一套
可反复重置的独立数据环境，但**不复制开发库里的历史任务**（首次为���库）。

本模块提供演示环境的路径定义与三个能力：

    1. :func:`activate_demo_paths` —— 在**导入业务模块之前**把全部落点切到演示目录；
    2. CLI ``status`` / ``init`` / ``backup`` / ``reset`` —— 只读查看、建空库、
       保存初始快照、恢复快照并同步附件。

安全纪律（硬性）
----------------
1. 所有可写目标必须位于演示目录（``data/demo/``）之内；
   ``data/campusflow.db``、``data/`` 本身、开发附件目录等一律拒绝并非零退出。
2. 备份与恢复一律使用 SQLite 官方 :meth:`sqlite3.Connection.backup`，
   由 SQLite 自行合并 ``-wal`` 内容，**绝不复制正在使用中的主库文件**，
   也不删除主库文件。
3. ``reset`` 前自动把当前演示库存一份 ``pre-reset-*.db``；
   基线快照不存在时**明确失败**，绝不以"清空数据库"代替恢复。
4. 附件清理只作用于校验通过的演示附件目录，且只删除"数据库中已无引用"的文件，
   逐个 ``unlink``，不递归、不触碰父目录。
5. 全部使用绝对路径；路径缺失 / 歧义 / 校验失败即中止，不做任何猜测。

用法::

    python docs/dev/demo_db.py status
    python docs/dev/demo_db.py init
    python docs/dev/demo_db.py backup
    python docs/dev/demo_db.py reset
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# --- 让本文件可以单独运行（与 docs/dev/_acceptance_app.py 相同的做法）---
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import sqlite_db  # noqa: E402

# --- 固定路径（均为绝对路径）---
DATA_DIR: Path = (PROJECT_ROOT / "data").resolve()
DEV_DB_PATH: Path = DATA_DIR / "campusflow.db"          # 开发库：永不作为目标
DEV_UPLOADS: Path = DATA_DIR / "uploads"                # 开发附件：永不作为目标
DEV_SUBMISSIONS: Path = DATA_DIR / "submissions"        # 开发附件：永不作为目标
DEMO_ROOT: Path = DATA_DIR / "demo"                     # 演示环境根目录

#: 需要统计的业务表（与 database/models.py 的 SCHEMA_STATEMENTS 一致）
MAIN_TABLES: tuple[str, ...] = (
    "tasks",
    "task_logs",
    "task_feedback",
    "task_submissions",
    "verifications",
    "task_closures",
    "task_metadata",
)


class DemoError(RuntimeError):
    """演示环境操作被拒绝（路径不安全、基线缺失、数据库被占用等）。

    统一用异常中止，调用方据此返回非零退出码——绝不"猜一个路径继续执行"。
    """


# ============================================================
# 一、演示环境路径
# ============================================================


@dataclass(frozen=True)
class DemoPaths:
    """演示环境的全部落点。root 之下的一切都属于演示环境。"""

    root: Path

    @property
    def db(self) -> Path:
        """演示数据库：data/demo/demo.db"""
        return self.root / "demo.db"

    @property
    def uploads(self) -> Path:
        """演示环境的上传材料目录。"""
        return self.root / "uploads"

    @property
    def submissions(self) -> Path:
        """演示环境的反馈 / 回执附件目录。"""
        return self.root / "submissions"

    @property
    def baseline(self) -> Path:
        """初始快照（基线）：验收通过后由 ``backup`` 生成。"""
        return self.root / "baseline" / "demo-baseline.db"

    @property
    def backup_dir(self) -> Path:
        """reset 前的自动备份目录。"""
        return self.root / "backups"

    def ensure_dirs(self) -> None:
        """创建演示目录树（幂等）。只创建演示目录，不创建任何开发目录。"""
        for directory in (self.root, self.uploads, self.submissions, self.baseline.parent, self.backup_dir):
            directory.mkdir(parents=True, exist_ok=True)


def _absolute(path: str | Path) -> Path:
    """把路径转成绝对路径（不要求存在）。"""
    return Path(path).expanduser().resolve()


def demo_paths(root: str | Path | None = None) -> DemoPaths:
    """返回演示环境路径集合；root 仅供测试或特殊部署覆盖，默认 data/demo。"""
    return DemoPaths(_absolute(root) if root is not None else DEMO_ROOT)


# --- 禁止触碰的开发资源（即使调用方显式指定也一律拒绝）---
PROTECTED_PATHS: tuple[Path, ...] = (DEV_DB_PATH, DEV_UPLOADS, DEV_SUBMISSIONS, DATA_DIR)


def assert_demo_root(root: str | Path | DemoPaths) -> Path:
    """校验演示根目录本身安全：不得是开发库、开发附件目录或项目 data 目录。"""
    target = root.root if isinstance(root, DemoPaths) else _absolute(root)
    for protected in PROTECTED_PATHS:
        if target == protected.resolve():
            raise DemoError(f"拒绝使用受保护路径作为演示目录：{target}")
    return target


def assert_demo_path(
    path: str | Path, *, root: str | Path | DemoPaths | None = None
) -> Path:
    """校验一个可写目标位于演示目录内，且不是任何受保护的开发资源。

    规则：
        1. 必须是绝对化后的确定路径；
        2. 必须位于演示根目录（默认 ``data/demo/``）之内；
        3. 不得等于开发库 / 开发附件目录 / 项目 data 目录。
    """
    target = _absolute(path)
    demo_root = assert_demo_root(root if root is not None else DEMO_ROOT)

    for protected in PROTECTED_PATHS:
        if target == protected.resolve():
            raise DemoError(f"拒绝操作开发资源：{target}")

    if target == demo_root:
        raise DemoError(f"目标是演示目录本身，请指定具体文件：{target}")

    if demo_root not in target.parents:
        raise DemoError(f"目标不在演示目录 {demo_root} 之内，已中止：{target}")
    return target


# ============================================================
# 二、路径切换（供 demo_app.py 在导入业务模块之前调用）
# ============================================================


def activate_demo_paths(root: str | Path | None = None) -> DemoPaths:
    """把全部落点切换到演示环境，并返回演示路径集合。

    必须在 ``from app import main`` **之前**调用，原因：
        - ``settings.DATA_DIR / UPLOAD_DIR / DB_PATH`` 是导入时求值的模块常量；
        - ``ui.pages.submission_view.SUBMISSION_DIR`` 同样是**导入时**求值
          （``SUBMISSION_DIR = settings.DATA_DIR / "submissions"``），
          因此必须先改 ``DATA_DIR``，再让 submission_view 被导入。

    本函数只做路径切换与目录创建，不写任何业务数据、不改任何业务规则。
    """
    paths = demo_paths(root)
    paths.ensure_dirs()

    from config import settings

    # 注意导入顺序：先设置 settings，再导入依赖它的模块
    settings.DATA_DIR = paths.root
    settings.UPLOAD_DIR = paths.uploads
    settings.DB_PATH = paths.db

    from ui.pages import submission_view

    sqlite_db.DB_PATH = paths.db
    submission_view.SUBMISSION_DIR = paths.submissions
    return paths


def init_demo_db(paths: DemoPaths | None = None) -> Path:
    """创建空的演示库与演示目录（幂等）。

    复用项目既有的 ``sqlite_db.init_db()``（即 SCHEMA_STATEMENTS 幂等建表），
    **不导入任何开发数据**，也不新增 / 修改任何表结构。
    """
    target = paths or demo_paths()
    assert_demo_path(target.db, root=target.root)
    target.ensure_dirs()
    sqlite_db.init_db(target.db)
    return target.db


# ============================================================
# 三、安全备份与恢复（SQLite Connection.backup，正确合并 WAL）
# ============================================================


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10.0)
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def safe_backup(src: str | Path, dst: str | Path, *, root: str | Path | None = None) -> Path:
    """用 SQLite 官方 backup API 把 ``src`` 安全复制到 ``dst``。

    为什么不用 ``shutil.copy``：库处于 WAL 模式时，最近提交的数据可能仍在
    ``-wal`` 里未 checkpoint，复制主库文件会得到"少数据"的快照。
    ``Connection.backup()`` 由 SQLite 逐页读取并自动合并 WAL，且不需要
    删除或覆盖目标文件，因此即使演示实例正在运行也不会破坏其数据。

    异常：DemoError 路径不安全；sqlite3.OperationalError 数据库被占用。
    """
    source = assert_demo_path(src, root=root)
    target = assert_demo_path(dst, root=root)
    if not source.exists():
        raise DemoError(f"源数据库不存在：{source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    src_conn = dst_conn = None
    try:
        src_conn = _connect(source)
        dst_conn = _connect(target)
        src_conn.backup(dst_conn)
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()
    return target


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ============================================================
# 四、只读查询
# ============================================================


def _open_readonly(path: Path) -> sqlite3.Connection:
    """只读连接：不存在则返回 None，绝不创建数据库。"""
    if not path.exists():
        raise DemoError(f"数据库不存在：{path}")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)


def table_counts(db_path: Path) -> dict[str, int | str]:
    """只读统计主要表行数；表缺失时返回 ``未创建``。"""
    counts: dict[str, int | str] = {}
    connection = _open_readonly(db_path)
    try:
        for table in MAIN_TABLES:
            exists = connection.execute(
                "select 1 from sqlite_master where type='table' and name = ?", (table,)
            ).fetchone()
            if not exists:
                counts[table] = "未创建"
                continue
            counts[table] = int(connection.execute(f"select count(*) from {table}").fetchone()[0])
    finally:
        connection.close()
    return counts


def dir_summary(directory: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)；目录不存在时返回 (0, 0)。"""
    if not directory.is_dir():
        return 0, 0
    files = [p for p in directory.iterdir() if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def referenced_attachment_names(db_path: Path) -> set[str]:
    """读取数据库中被引用的附件文件名集合（只取文件名，忽略目录差异）。"""
    names: set[str] = set()
    connection = _open_readonly(db_path)
    try:
        for table in ("task_feedback", "task_submissions"):
            exists = connection.execute(
                "select 1 from sqlite_master where type='table' and name = ?", (table,)
            ).fetchone()
            if not exists:
                continue
            for (value,) in connection.execute(f"select file_path from {table}"):
                if value:
                    names.add(Path(str(value)).name)
    finally:
        connection.close()
    return names


# ============================================================
# 五、CLI 子命令
# ============================================================


def cmd_status(paths: DemoPaths) -> int:
    """只读展示演示环境状态（不创建、不修改任何文件）。"""
    print("=" * 68)
    print("CampusFlow AI 演示环境状态（只读）")
    print("=" * 68)
    print(f"演示根目录  : {paths.root}")
    print(f"演示数据库  : {paths.db}" + ("" if paths.db.exists() else "   [尚未创建]"))
    print(f"上传材料目录: {paths.uploads}")
    print(f"附件目录    : {paths.submissions}")
    print(f"基线快照    : {paths.baseline}" + ("" if paths.baseline.exists() else "   [尚未保存]"))
    print(f"自动备份目录: {paths.backup_dir}")
    print("-" * 68)
    print(f"开发库(只读参照，本次不会写入): {DEV_DB_PATH}")
    print(f"开发库是否存在: {DEV_DB_PATH.exists()}")

    if paths.db.exists():
        print("-" * 68)
        print("演示库主要表行数：")
        for table, value in table_counts(paths.db).items():
            print(f"  - {table:<18}: {value}")
        for label, directory in (("上传材料", paths.uploads), ("反馈/回执附件", paths.submissions)):
            count, size = dir_summary(directory)
            print(f"{label}: {count} 个文件 / {size} 字节  ({directory})")
        wal = paths.db.with_name(paths.db.name + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            print(
                f"提示：检测到非空 WAL 文件（{wal.name}，{wal.stat().st_size} 字节），"
                "演示实例可能仍在运行；恢复前请先停止实例。"
            )
    else:
        print("演示库尚未创建。首次运行 demo_app.py 或执行 `init` 会自动创建**空库**。")

    if paths.baseline.exists():
        print(f"基线快照已存在：{paths.baseline}（{paths.baseline.stat().st_size} 字节）")
    else:
        print("尚未保存基线快照：验收通过后执行 `backup` 保存，之后可用 `reset` 反复恢复。")
    return 0


def cmd_init(paths: DemoPaths) -> int:
    """创建空的演示库与演示目录（幂等，不导入开发数据）。"""
    db_path = init_demo_db(paths)
    print(f"已就绪：{db_path}")
    print(f"演示目录  ：{paths.root}")
    print("提示：演示库为**空库**，不含任何开发库的历史任务。")
    return 0


def cmd_backup(paths: DemoPaths, *, force: bool = False) -> int:
    """把当前演示数据保存为初始快照（基线）。"""
    if not paths.db.exists():
        raise DemoError(f"演示数据库尚未创建：{paths.db}（请先运行 demo_app.py 或执行 init）")
    if paths.baseline.exists() and not force:
        raise DemoError(
            f"基线快照已存在：{paths.baseline}\n"
            "如需覆盖请显式加 --force（会替换旧基线，不会影响演示库本身）。"
        )
    saved = safe_backup(paths.db, paths.baseline, root=paths.root)
    print(f"基线快照已保存：{saved}（{saved.stat().st_size} 字节）")
    print("后续录制前执行 `reset` 即可回到该状态。")
    return 0


def _sync_attachments(paths: DemoPaths) -> tuple[int, int]:
    """删除演示目录中"数据库已无引用"的附件文件，返回 (删除数, 保留数)。

    只遍历 ``paths.uploads`` 与 ``paths.submissions`` 两个**校验通过**的目录，
    只删普通文件（逐个 unlink），不递归、不删除目录本身。
    """
    keep = referenced_attachment_names(paths.db)
    removed = kept = 0
    for directory in (paths.uploads, paths.submissions):
        if not directory.is_dir():
            continue
        for item in sorted(directory.iterdir()):
            if not item.is_file():
                continue
            if item.name in keep:
                kept += 1
                continue
            item.unlink()
            removed += 1
    return removed, kept


def cmd_reset(paths: DemoPaths, *, baseline: str | Path | None = None) -> int:
    """把演示库恢复到基线快照，并清理与基线不一致的附件。

    步骤：校验基线 → 自动备份当前演示库 → backup() 恢复 → 同步附件。
    基线缺失时**明确失败**，绝不以"清空数据库"代替恢复。
    """
    if not paths.db.exists():
        raise DemoError(f"演示数据库尚未创建：{paths.db}（请先运行 demo_app.py 或执行 init）")

    source = _absolute(baseline) if baseline is not None else paths.baseline
    source = assert_demo_path(source, root=paths.root)
    if not source.exists():
        raise DemoError(
            f"基线快照不存在：{source}\n"
            "请先在验收通过后执行：python docs/dev/demo_db.py backup"
        )

    # 1) reset 前自动备份当前演示库（不删除任何既有备份）
    keep_backup = safe_backup(
        paths.db, paths.backup_dir / f"pre-reset-{_timestamp()}.db", root=paths.root
    )
    print(f"已自动备份当前演示库：{keep_backup}")

    # 2) 恢复：backup() 逐页写回，不删除主库文件
    try:
        safe_backup(source, paths.db, root=paths.root)
    except sqlite3.OperationalError as exc:
        raise DemoError(
            f"恢复失败：{exc}\n"
            "演示数据库可能被正在运行的实例占用。请先停止演示实例"
            "（streamlit run docs/dev/demo_app.py --server.port 8502）再执行 reset。"
        ) from exc
    print(f"已从基线恢复：{source}")

    # 3) 附件同步：只删数据库中已无引用的文件
    removed, kept = _sync_attachments(paths)
    print(f"附件同步：删除 {removed} 个无引用文件，保留 {kept} 个已引用文件")
    print("演示环境已恢复到基线状态，可以重新开始录制。")
    return 0


# ============================================================
# 六、入口
# ============================================================

_COMMANDS = {
    "status": cmd_status,
    "init": cmd_init,
    "backup": cmd_backup,
    "reset": cmd_reset,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo_db.py",
        description="CampusFlow AI 演示环境数据管理（只允许操作 data/demo/ 内的演示数据）",
    )
    parser.add_argument("--root", default=None, help="演示根目录（默认 data/demo，仅测试或特殊部署需要）")
    parser.add_argument(
        "--baseline", default=None, help="基线快照绝对路径（默认 <root>/baseline/demo-baseline.db）"
    )
    parser.add_argument("--force", action="store_true", help="backup 时允许覆盖既有基线快照")
    parser.add_argument("command", choices=sorted(_COMMANDS))
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    paths = demo_paths(args.root)
    try:
        if args.command == "backup":
            return cmd_backup(paths, force=args.force)
        if args.command == "reset":
            return cmd_reset(paths, baseline=args.baseline)
        return _COMMANDS[args.command](paths)
    except DemoError as exc:
        print(f"[拒绝执行] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())