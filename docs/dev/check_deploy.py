# -*- coding: utf-8 -*-
"""开发期临时脚本：核查部署相关状态（密钥脱敏、SQLite 现状、docs 与上传目录）。

说明：
- 本脚本仅用于开发/部署排错，**不参与应用运行**，也不被任何模块导入；
- 已从项目根目录移入 docs/dev/，避免提交源码时根目录出现临时文件；
- 项目根目录由脚本自身位置推导（docs/dev/ → 上两级），换机器也能直接运行。

用法：python docs/dev/check_deploy.py
"""

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 项目根目录：本文件位于 <root>/docs/dev/check_deploy.py
BASE = Path(__file__).resolve().parents[2]

# ---------- 1. .env 配置（脱敏） ----------
print("=" * 60)
print("[1] .env 配置（API Key 已脱敏）")
print("=" * 60)
env_file = BASE / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if any(token in key.upper() for token in ("KEY", "SECRET", "TOKEN")):
                print(f"{key} = {'*' * 8 if value else '（空）'}（长度 {len(value)}）")
            else:
                print(f"{key} = {value}")
else:
    print("（.env 不存在）")

print(f".env.example 存在: {(BASE / '.env.example').exists()}")

# ---------- 2. SQLite 数据库现状 ----------
print()
print("=" * 60)
print("[2] SQLite 数据库现状（Agent Memory）")
print("=" * 60)
import sqlite3

db_path = BASE / "data" / "campusflow.db"
if not db_path.exists():
    print(f"数据库文件不存在: {db_path}")
    sys.exit(0)

print(f"数据库文件: {db_path}")
print(f"文件大小: {db_path.stat().st_size / 1024:.1f} KB")

conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
try:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    print(f"数据表: {tables}")

    task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    log_count = conn.execute("SELECT COUNT(*) FROM task_logs").fetchone()[0]
    print(f"tasks 记录数: {task_count}")
    print(f"task_logs 记录数: {log_count}")

    if task_count:
        print("状态分布:")
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status ORDER BY n DESC"):
            print(f"  {row['status']}: {row['n']}")

        print("部门分布:")
        for row in conn.execute(
            "SELECT department, COUNT(*) AS n FROM tasks WHERE department IS NOT NULL "
            "GROUP BY department ORDER BY n DESC"
        ):
            print(f"  {row['department']}: {row['n']}")

        print("任务样例（前 5 条）:")
        for row in conn.execute(
            "SELECT id, title, status, priority, risk_level FROM tasks ORDER BY id LIMIT 5"
        ):
            print(
                f"  #{row['id']} {row['title']} | status={row['status']} "
                f"priority={row['priority']} risk={row['risk_level']}"
            )

        print("日志样例（最近 5 条）:")
        for row in conn.execute(
            "SELECT task_id, action, operator, created_at FROM task_logs ORDER BY id DESC LIMIT 5"
        ):
            print(
                f"  task#{row['task_id']} action={row['action']} "
                f"operator={row['operator']} at={row['created_at']}"
            )
finally:
    conn.close()

# ---------- 3. docs 目录 ----------
print()
print("=" * 60)
print("[3] docs 目录内容")
print("=" * 60)
docs_dir = BASE / "docs"
if docs_dir.exists():
    print(f"docs/ 下共 {len(list(docs_dir.iterdir()))} 个条目: {[e.name for e in docs_dir.iterdir()]}")
else:
    print("docs/ 目录不存在")

# ---------- 4. 上传件目录 ----------
print()
print("=" * 60)
print("[4] data/uploads 上传件")
print("=" * 60)
uploads = BASE / "data" / "uploads"
if uploads.exists():
    for item in sorted(uploads.iterdir()):
        print(f"  {item.name}（{item.stat().st_size / 1024:.1f} KB）")
else:
    print("（uploads 目录不存在）")
