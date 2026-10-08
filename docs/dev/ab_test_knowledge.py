"""开发期脚本：知识库注入对「责任部门」抽取影响的对照实验。

对同一批材料分别执行：
  A 组：注入部门职责知识库（系统默认，organization=None）
  B 组：禁用知识库（organization=""，仅用基础 Prompt）
对比 department 字段差异，用于定位「部门被归一化 / 丢失」的根因。

输出：docs/dev/_abtest.json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import task_agent  # noqa: E402

PARSED_DIR = Path(__file__).resolve().parent / "_parsed"
OUT_FILE = Path(__file__).resolve().parent / "_abtest.json"

# 代表性材料：覆盖会议纪要、Excel 台账、公文通知三种来源
TARGETS = [
    "会议和综合事务__Word 格式会议纪要.docx.txt",
    "学生工作与信息缺失__学生事务重点工作推进台账.xlsx.txt",
    "安全和高风险事务__校园消防安全检查工作安排.docx.txt",
    "学生工作与信息缺失__毕业生就业工作安排.docx.txt",
]


def main() -> None:
    report: dict = {}
    for name in TARGETS:
        path = PARSED_DIR / name
        if not path.is_file():
            print(f"[跳过] 文件不存在：{name}")
            continue
        text = path.read_text(encoding="utf-8")
        source_type = "excel" if name.endswith(".xlsx.txt") else "document"
        print(f"\n=== {name} ===")

        print("  [A] 注入知识库")
        started = time.perf_counter()
        with_kb = task_agent.extract_tasks(text, source_type=source_type, organization=None)
        print(f"    用时 {time.perf_counter() - started:.1f}s，提取 {len(with_kb)} 条")

        print("  [B] 禁用知识库")
        started = time.perf_counter()
        without_kb = task_agent.extract_tasks(text, source_type=source_type, organization="")
        print(f"    用时 {time.perf_counter() - started:.1f}s，提取 {len(without_kb)} 条")

        report[name] = {"with_knowledge": with_kb, "without_knowledge": without_kb}

    OUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成，结果写入：{OUT_FILE}")


if __name__ == "__main__":
    main()