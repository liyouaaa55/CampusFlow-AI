"""开发期脚本：把 TaskAgent 识别结果与人工基准逐项比对，输出结构化差异。

仅用于本次测试评估，不参与应用运行。
输入：docs/dev/_ground_truth.json + docs/dev/_results/*.json
输出：控制台 Markdown 对比表 + docs/dev/_compare.json
"""

from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
GT_FILE = BASE / "_ground_truth.json"
RESULT_DIR = BASE / "_results"
OUT_FILE = BASE / "_compare.json"


def chars(text: str) -> set[str]:
    """取单字集合。标题措辞差异较大时（如「核对修正学生系统登记信息」与
    「学生信息核对与线上修正」实为同一条事项），2-gram 相似度会严重低估，
    因此改用单字集合。"""
    return {ch for ch in text if ch.strip()}


def similarity(left: str, right: str) -> float:
    """重叠系数：交集 / 较小集合，对「长短表述同一事项」更宽容。"""
    a, b = chars(left), chars(right)
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


MATCH_THRESHOLD = 0.45


def match_tasks(expected: list[dict], actual: list[dict]) -> list[tuple[int | None, int | None, float]]:
    """贪心匹配：返回 [(期望索引, 实际索引, 相似度), ...]，
    相似度低于 0.25 视为未匹配。"""
    pairs: list[tuple[int | None, int | None, float]] = []
    used: set[int] = set()
    for ei, exp in enumerate(expected):
        best, best_score = None, 0.0
        for ai, act in enumerate(actual):
            if ai in used:
                continue
            score = similarity(exp.get("t", ""), act.get("title", ""))
            if score > best_score:
                best, best_score = ai, score
        if best is not None and best_score >= MATCH_THRESHOLD:
            used.add(best)
            pairs.append((ei, best, round(best_score, 2)))
        else:
            pairs.append((ei, None, round(best_score, 2)))
    for ai in range(len(actual)):
        if ai not in used:
            pairs.append((None, ai, 0.0))
    return pairs


def main() -> None:
    ground_truth = json.loads(GT_FILE.read_text(encoding="utf-8"))
    report: dict = {}

    for name, gt in ground_truth.items():
        if name.startswith("_"):
            continue
        # batch_parse 输出的文本名为「原文件名.txt」，识别结果沿用该名
        result_path = RESULT_DIR / f"{name}.txt.json"
        if not result_path.is_file():
            report[name] = {"error": "缺少识别结果文件"}
            continue

        payload = json.loads(result_path.read_text(encoding="utf-8"))
        actual = payload.get("tasks") or []
        expected = gt.get("items") or []

        matched = match_tasks(expected, actual)
        misses, extras = [], []
        dept_stat = {"一致": 0, "归一化改写": 0, "丢失为null": 0, "杜撰": 0}
        deadline_stat = {"一致": 0, "不一致": 0, "期望null但有值": 0}
        owner_stat = {"一致": 0, "不一致": 0, "期望null但有值": 0}

        for ei, ai, _score in matched:
            if ei is None:
                extras.append(actual[ai])
                continue
            if ai is None:
                misses.append(expected[ei])
                continue

            exp, act = expected[ei], actual[ai]

            # 责任部门
            exp_d, act_d = exp.get("d"), act.get("department")
            if exp_d and act_d == exp_d:
                dept_stat["一致"] += 1
            elif act_d is None:
                dept_stat["丢失为null"] += 1
            elif not exp_d:
                dept_stat["一致"] += 1
            else:
                dept_stat["归一化改写"] += 1

            # 截止日期
            exp_dl, act_dl = exp.get("dl"), act.get("deadline")
            if exp_dl == act_dl:
                deadline_stat["一致"] += 1
            elif exp_dl and not act_dl:
                deadline_stat["不一致"] += 1
            elif not exp_dl and act_dl:
                deadline_stat["期望null但有值"] += 1
            else:
                deadline_stat["不一致"] += 1

            # 负责人
            exp_o, act_o = exp.get("o"), act.get("owner")
            if exp_o == act_o:
                owner_stat["一致"] += 1
            elif exp_o and not act_o:
                owner_stat["不一致"] += 1
            elif not exp_o and act_o:
                owner_stat["期望null但有值"] += 1
            else:
                owner_stat["不一致"] += 1

        # 明确禁止出现的内容（幻觉检查）
        blob = " ".join(
            f"{t.get('title', '')} {t.get('description', '')}" for t in actual
        )
        forbid_hits = [word for word in gt.get("forbid", []) if word and word in blob]

        count_ok = gt.get("min", 0) <= len(actual) <= gt.get("max", 99)
        priorities: dict[str, int] = {}
        for item in actual:
            key = item.get("priority") or "null"
            priorities[key] = priorities.get(key, 0) + 1

        report[name] = {
            "expected_range": [gt.get("min"), gt.get("max")],
            "actual_count": len(actual),
            "count_ok": count_ok,
            "missed": [item.get("t") for item in misses],
            "extra": [item.get("title") for item in extras],
            "department": dept_stat,
            "deadline": deadline_stat,
            "owner": owner_stat,
            "priority_dist": priorities,
            "forbid_hits": forbid_hits,
            "elapsed_sec": payload.get("elapsed_sec"),
            "trap": gt.get("trap"),
        }

    OUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 控制台对比表 ----------------
    print("=" * 112)
    print(
        f"{'材料':<44}{'期望':>7}{'实际':>6}{'漏':>5}{'多':>5}"
        f"{'日期错':>7}{'部门改写':>9}{'部门丢':>7}{'负责人错':>9}"
    )
    print("=" * 112)
    for name, row in report.items():
        short = name.split("__")[-1][:42]
        if "error" in row:
            print(f"{short:<44}{'读取失败':>7}")
            continue
        rng = f"{row['expected_range'][0]}-{row['expected_range'][1]}"
        mark = "!" if not row["count_ok"] else " "
        print(
            f"{short:<44}{rng:>7}{row['actual_count']:>5}{mark}"
            f"{len(row['missed']):>5}{len(row['extra']):>5}"
            f"{row['deadline']['不一致']:>7}{row['department']['归一化改写']:>9}"
            f"{row['department']['丢失为null']:>7}"
            f"{row['owner']['不一致'] + row['owner']['期望null但有值']:>9}"
        )
    print("=" * 112)

    total_miss = sum(len(r.get("missed", [])) for r in report.values())
    total_extra = sum(len(r.get("extra", [])) for r in report.values())
    print(f"合计：漏识别 {total_miss} 条，多余/幻觉 {total_extra} 条")

    forbid_any = {n: r["forbid_hits"] for n, r in report.items() if r.get("forbid_hits")}
    if forbid_any:
        print("\n命中「禁止内容」（幻觉风险）：")
        for name, hits in forbid_any.items():
            print(f"  - {name.split('__')[-1]}：{hits}")

    print(f"\n明细已写入：{OUT_FILE}")


if __name__ == "__main__":
    main()