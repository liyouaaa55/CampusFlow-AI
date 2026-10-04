# -*- coding: utf-8 -*-
"""开发期临时脚本：解析 data/uploads 下的比赛相关上传件，提取文本内容。

说明：
- 本脚本仅用于开发期核对比赛材料，**不参与应用运行**，也不被任何模块导入；
- 已从项目根目录移入 docs/dev/，避免提交源码时根目录出现临时文件；
- 项目根目录由脚本自身位置推导（docs/dev/ → 上两级）。

用法：python docs/dev/extract_uploads.py
"""

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parents[2]
UPLOADS = BASE / "data" / "uploads"


def extract_docx() -> None:
    """解析上传目录中的 Word 文档。"""
    print("=" * 60)
    print("[1] Word 文档内容")
    print("=" * 60)

    candidates = sorted(UPLOADS.glob("*.docx"))
    if not candidates:
        print("（未找到 .docx 文件）")
        return

    try:
        import docx
    except ImportError:
        print("未安装 python-docx，跳过。")
        return

    for path in candidates:
        print(f"--- {path.name} ---")
        try:
            document = docx.Document(str(path))
            for paragraph in document.paragraphs:
                if paragraph.text.strip():
                    print(paragraph.text)
            for table in document.tables:
                print("--- 表格 ---")
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        print(" | ".join(cells))
        except Exception as exc:  # 单个文件解析失败不影响其他文件
            print(f"解析失败: {type(exc).__name__}: {exc}")


def extract_xlsx() -> None:
    """解析上传目录中的 Excel 表格。"""
    print()
    print("=" * 60)
    print("[2] Excel 表格内容")
    print("=" * 60)

    candidates = sorted(UPLOADS.glob("*.xlsx"))
    if not candidates:
        print("（未找到 .xlsx 文件）")
        return

    try:
        import pandas as pd
    except ImportError:
        print("未安装 pandas，跳过。")
        return

    for path in candidates:
        print(f"--- {path.name} ---")
        try:
            frame = pd.read_excel(path, header=None, dtype=str)
            for _, row in frame.iterrows():
                cells = [str(value).strip() for value in row.tolist() if str(value).strip() not in ("", "nan")]
                if cells:
                    print(" | ".join(cells))
        except Exception as exc:
            print(f"解析失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    extract_docx()
    extract_xlsx()
