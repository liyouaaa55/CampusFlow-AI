"""开发期脚本：批量调用 TaskAgent 对测试资料做事项识别，输出结构化结果。

仅用于本次测试评估，不参与应用运行。
输入：docs/dev/_parsed/*.txt
输出：docs/dev/_results/<同名>.json
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import task_agent  # noqa: E402
from config import settings  # noqa: E402

PARSED_DIR = Path(__file__).resolve().parent / "_parsed"
RESULT_DIR = Path(__file__).resolve().parent / "_results"

# 会议纪要类材料按「会议纪要」来源识别，其余按扩展名推断
MEETING_KEYWORDS = ("会议纪要", "工作会议纪要")
SUFFIX_TO_SOURCE = {".docx": "document", ".xlsx": "excel", ".txt": "text"}


def guess_source_type(txt_path: Path) -> str:
    """按原文件扩展名 + 文件名推断 source_type。"""
    if any(key in txt_path.name for key in MEETING_KEYWORDS):
        return "meeting"
    for suffix, source in SUFFIX_TO_SOURCE.items():
        if txt_path.name.endswith(f"{suffix}.txt"):
            return source
    return "text"


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"模型：{settings.DEEPSEEK_MODEL}  地址：{settings.DEEPSEEK_BASE_URL}")
    print(f"LLM 可用：{settings.is_llm_ready()}\n")

    files = sorted(PARSED_DIR.glob("*.txt"))
    summary: list[dict] = []

    for path in files:
        text = path.read_text(encoding="utf-8")
        source_type = guess_source_type(path)
        started = time.perf_counter()
        try:
            tasks = task_agent.extract_tasks(text, source_type=source_type)
            error = None
        except Exception as exc:  # noqa: BLE001
            tasks = []
            error = f"{type(exc).__name__}: {exc}"
            print(traceback.format_exc())
        elapsed = round(time.perf_counter() - started, 1)

        payload = {
            "name": path.name.replace(".txt", ""),
            "source_type": source_type,
            "chars": len(text),
            "elapsed_sec": elapsed,
            "error": error,
            "count": len(tasks),
            "tasks": tasks,
        }
        (RESULT_DIR / f"{path.name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary.append(payload)
        flag = "ERR" if error else "OK "
        print(f"[{flag}] {elapsed:>5}s | 提取条数={len(tasks):>2} | {path.name}")

    ok = sum(1 for item in summary if not item["error"])
    print(f"\n完成：{ok}/{len(summary)} 个文件识别成功，"
          f"合计提取事项 {sum(item['count'] for item in summary)} 条")


if __name__ == "__main__":
    main()