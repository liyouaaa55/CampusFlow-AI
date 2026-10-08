"""开发期脚本：批量解析测试资料目录中的所有文件，导出纯文本供人工核对。

仅用于本次测试评估，不参与应用运行。
输出目录：docs/dev/_parsed/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools import parser_tool  # noqa: E402

SOURCE_DIR = Path(r"C:\Users\liyou\Desktop\测试数据")
OUT_DIR = Path(__file__).resolve().parent / "_parsed"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []

    files = sorted(p for p in SOURCE_DIR.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(SOURCE_DIR)
        # 输出文件名：类别__原名.txt
        flat = "__".join(rel.parts)
        target = OUT_DIR / f"{flat}.txt"
        entry: dict = {
            "category": rel.parts[0] if len(rel.parts) > 1 else "(根目录)",
            "filename": path.name,
            "suffix": path.suffix.lower(),
            "bytes": path.stat().st_size,
            "out": target.name,
        }
        try:
            text = parser_tool.parse_file(path)
            target.write_text(text, encoding="utf-8")
            entry["chars"] = len(text)
            entry["lines"] = len(text.split("\n")) if text else 0
            entry["parse"] = "ok" if text.strip() else "EMPTY"
        except Exception as exc:  # noqa: BLE001
            entry["chars"] = 0
            entry["lines"] = 0
            entry["parse"] = f"ERROR: {type(exc).__name__}: {exc}"
        manifest.append(entry)
        print(f"{entry['parse']:>8} | {entry['chars']:>6}字 | {rel}")

    (OUT_DIR / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n共 {len(manifest)} 个文件，输出目录：{OUT_DIR}")


if __name__ == "__main__":
    main()