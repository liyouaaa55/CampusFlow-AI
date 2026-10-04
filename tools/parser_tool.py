"""文档解析工具：把 Word / PDF / TXT / Excel 统一转成纯文本。

设计要点：
- 只做「文件 → 字符串」的转换，不涉及任何业务判断；
- 第三方解析库在函数内部按需导入，未安装对应库时给出明确提示；
- 输出统一经过 normalize_text()，避免把多余空白交给大模型。
"""

from __future__ import annotations

import re
from pathlib import Path

# 扩展名 → source_type 映射
# 注意：meeting（会议纪要）语义上无法由扩展名判断，需由调用方显式指定
EXTENSION_SOURCE_TYPES: dict[str, str] = {
    ".docx": "document",
    ".doc": "document",
    ".pdf": "document",
    ".txt": "text",
    ".md": "text",
    ".xlsx": "excel",
    ".xls": "excel",
    ".csv": "excel",
}


def detect_source_type(filename: str | Path) -> str:
    """依据文件扩展名推断来源类型；无法判断时按纯文本处理。"""
    return EXTENSION_SOURCE_TYPES.get(Path(filename).suffix.lower(), "text")


def normalize_text(text: str | None) -> str:
    """规范化文本：统一换行、压缩行内空白、折叠多余空行、去除首尾空白。"""
    if not text:
        return ""

    # 统一换行符，并把全角空格转为普通空格
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")

    lines: list[str] = []
    for line in cleaned.split("\n"):
        # 压缩行内连续空格/制表符
        line = re.sub(r"[ \t]+", " ", line).strip()
        # 折叠连续空行
        if not line and lines and lines[-1] == "":
            continue
        lines.append(line)

    return "\n".join(lines).strip()


def parse_file(file_path: str | Path) -> str:
    """解析文件为纯文本。

    支持 .docx / .pdf / .txt / .md / .xlsx / .xls / .csv。
    异常：文件不存在抛 FileNotFoundError，格式不支持抛 ValueError。
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")

    suffix = path.suffix.lower()
    if suffix == ".docx":
        raw = _parse_docx(path)
    elif suffix == ".pdf":
        raw = _parse_pdf(path)
    elif suffix in (".xlsx", ".xls", ".csv"):
        raw = _parse_excel(path)
    elif suffix in (".txt", ".md"):
        raw = _read_text_file(path)
    else:
        raise ValueError(f"不支持的文件格式：{suffix or '（无扩展名）'}")

    return normalize_text(raw)


# ------------------------------------------------------------
# 各格式的具体解析函数（内部使用）
# ------------------------------------------------------------


def _parse_docx(path: Path) -> str:
    """解析 Word：按顺序提取段落与表格内容。"""
    try:
        from docx import Document
    except ImportError as exc:  # 提示安装依赖，避免报无头绪的错误
        raise ImportError("解析 .docx 需要 python-docx，请执行：pip install python-docx") from exc

    document = Document(str(path))
    blocks: list[str] = [paragraph.text for paragraph in document.paragraphs]

    # 表格内容按「单元格 | 单元格」拼成一行，保留行列关系
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            line = " | ".join(cell for cell in cells if cell)
            if line:
                blocks.append(line)

    return "\n".join(blocks)


def _parse_pdf(path: Path) -> str:
    """解析 PDF：逐页抽取文本。"""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("解析 .pdf 需要 pdfplumber，请执行：pip install pdfplumber") from exc

    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _parse_excel(path: Path) -> str:
    """解析 Excel / CSV：每行用「 | 」拼接，转为可读文本。"""
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("解析表格需要 pandas，请执行：pip install pandas openpyxl") from exc

    # header=None：保留表头行，避免丢失信息；dtype=str：避免数字被改写
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path, header=None, dtype=str)
    else:
        frame = pd.read_excel(path, header=None, dtype=str)

    lines: list[str] = []
    for _, row in frame.iterrows():
        cells = [str(value).strip() for value in row.tolist() if str(value).strip() not in ("", "nan")]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _read_text_file(path: Path) -> str:
    """读取纯文本文件，依次尝试 utf-8 / gbk 编码。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    # 两种编码都失败时，忽略非法字符强制读取，保证流程不中断
    return path.read_text(encoding="utf-8", errors="ignore")
