"""一键运行时就绪自检（赛前/演示前执行）。

用法：
    # 快速自检（不发起网络请求，毫秒级）
    .venv\\Scripts\\python.exe docs\\dev\\系统自检.py

    # 深度自检（真实调用大模型，验证密钥与端点，秒级）
    .venv\\Scripts\\python.exe docs\\dev\\系统自检.py --deep

退出码：
    0 = 无阻断项，具备演示条件
    1 = 存在阻断项，暂不具备演示条件

设计说明：
    本脚本只做检查，不修改任何业务数据；所有写操作都在临时目录进行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接以脚本方式运行：把项目根目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.preflight import render_text, run_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="CampusFlow AI 运行时就绪自检")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="真实调用大模型（验证密钥、端点与识别/分析链路），耗时更长",
    )
    args = parser.parse_args()

    try:
        report = run_all(deep=args.deep)
    except Exception as exc:  # noqa: BLE001
        print(f"自检自身异常终止：{type(exc).__name__}: {exc}")
        return 1

    print(render_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
