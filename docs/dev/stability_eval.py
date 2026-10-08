"""LLM 抽取稳定性评测（只读观测脚本，不修改任何生产代码）。

用途
----
回答一个问题：**同一份材料、同一份 Prompt、同一组参数，连续调用 N 次，
任务抽取结果的波动到底有多大？波动发生在哪一层？**

设计要点（对应验收要求）
------------------------
1. **每份材料只解析一次**，固定 parsed_text，5 次调用共用同一份输入，
   从而把「文件解析波动」排除在测量之外；
2. 通过注入捕获型 llm_fn 调用 **生产代码路径本身**
   （agent.task_agent.extract_tasks），不复制、不改写任何业务逻辑；
3. 每次调用保存四层数据：原始 LLM response / 解析后的列表 / normalize 后的任务 /
   实际使用的 prompt，用于区分波动发生在哪一层；
4. 记录 parsed_text 的 sha256，证明 5 次输入完全一致；
5. 任务对齐用「规范化 title 精确匹配 + 相似度兜底」，不按数组下标比对。

用法
----
    python docs/dev/stability_eval.py --run 8 7
    python docs/dev/stability_eval.py --run 13 2 9
    python docs/dev/stability_eval.py --report

产物
----
    docs/dev/_stability/raw_<id>.json    原始逐次数据
    docs/dev/stability_report.md         稳定性报告
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# 让脚本能以 `python docs/dev/stability_eval.py` 直接运行
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import task_agent  # noqa: E402
from config import settings  # noqa: E402
from llm import client, prompts  # noqa: E402
from tools import parser_tool  # noqa: E402

SRC = Path(r"C:\Users\liyou\Desktop\测试数据")
OUT_DIR = ROOT / "docs" / "dev" / "_stability"
REPORT = ROOT / "docs" / "dev" / "stability_report.md"
RUNS = 5


def set_runs(n: int) -> None:
    """调整单份材料的调用次数（供 --runs 覆盖）。"""
    global RUNS
    RUNS = n

# ------------------------------------------------------------------
# 材料清单
# ------------------------------------------------------------------
MATERIALS: dict[int, dict[str, str]] = {
    2: {"cat": "安全和高风险事务", "name": "校园消防安全检查工作安排.docx", "st": "document",
        "focus": "正常任务识别是否稳定、任务数量是否稳定"},
    7: {"cat": "活动、复杂材料和无任务材料",
        "name": "3.校园运动会筹备任务一览表，测试Excel 中的多任务、多状态识别.xlsx", "st": "excel",
        "focus": "completed 行是否偶发丢失、行与任务是否一一对应"},
    8: {"cat": "活动、复杂材料和无任务材料",
        "name": "4.校园运动会筹备与宣传工作进展情况 测试如果材料本身没有需要执行的事项CampusFlow AI 能不能不乱生成任务(1).txt",
        "st": "text", "focus": "「下一步将抓好……」是否稳定误拆、任务数量是否波动"},
    9: {"cat": "会议和综合事务", "name": "Word 格式综合工作会议纪要.docx", "st": "meeting",
        "focus": "会议决策事项是否稳定、是否发生额外生成或丢失"},
    13: {"cat": "学生工作与信息缺失", "name": "学生事务重点工作推进台账.xlsx", "st": "excel",
         "focus": "总任务数是否波动、completed 行是否丢失、owner/status 是否变化"},
}

# 重点追踪项（子串匹配 title）
WATCH: dict[int, dict[str, list[str]]] = {
    8: {"方向性误拆": ["场地联合验收", "秩序册", "志愿者培训", "开闭幕式彩排"]},
    7: {"已完成行": ["领导小组", "主题口号", "经费预算"]},
    13: {"已完成行": ["就业信息初步核查"]},
}

STATUS_KEYS = ("pending", "processing", "completed", "cancelled")


# ------------------------------------------------------------------
# 采集
# ------------------------------------------------------------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def collect(mid: int) -> dict[str, Any]:
    """对一份材料连续调用 RUNS 次，保存四层数据。"""
    meta = MATERIALS[mid]
    path = SRC / meta["cat"] / meta["name"]

    # ① 只解析一次，全轮次共用
    parsed_text = parser_tool.parse_file(path)
    input_hash = _sha(parsed_text)

    records: list[dict[str, Any]] = []
    for run_id in range(1, RUNS + 1):
        captured: dict[str, Any] = {}

        def capture(system_prompt: str, user_prompt: str, _c=captured) -> Any:
            raw = client.chat_json(system_prompt, user_prompt)
            _c["raw"] = raw
            _c["system"] = system_prompt
            _c["user"] = user_prompt
            return raw

        # 单次失败（如模型返回畸形 JSON）不应中断整轮测量：
        # 失败本身也是一种观测结果，须原样记录。
        error: str | None = None
        tasks: list[dict[str, Any]] = []
        try:
            tasks = task_agent.extract_tasks(
                parsed_text, source_type=meta["st"], llm_fn=capture
            )
        except Exception as exc:  # noqa: BLE001 - 观测脚本，需记录任意失败
            error = f"{type(exc).__name__}: {exc}"

        as_items = task_agent._as_items(captured["raw"]) if "raw" in captured else []
        as_list: list[dict[str, Any]] = [x for x in as_items if isinstance(x, dict)]

        records.append({
            "run_id": run_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "system_hash": _sha(captured.get("system", "")),
            "user_hash": _sha(captured.get("user", "")),
            "input_hash": _sha(captured.get("user", "")),
            "error": error,
            "raw_response": captured.get("raw"),
            "parsed_items": as_list,
            "normalized_tasks": tasks,
        })
        flag = f"  !! {error[:60]}" if error else ""
        print(f"    run{run_id}: {len(tasks):2d} 项{flag}", flush=True)

    return {
        "material_id": mid,
        "file_name": meta["name"],
        "source_type": meta["st"],
        "focus": meta["focus"],
        "parsed_chars": len(parsed_text),
        "parsed_input_sha256_16": input_hash,
        "prompt_system_sha256_16": _sha(prompts.TASK_EXTRACTION_SYSTEM),
        "model": settings.read_config("DEEPSEEK_MODEL"),
        "base_url": settings.read_config("DEEPSEEK_BASE_URL"),
        "temperature": prompts.TASK_EXTRACTION_USER and settings.LLM_TEMPERATURE,
        "runs": RUNS,
        "records": records,
    }


# ------------------------------------------------------------------
# 分析
# ------------------------------------------------------------------
def _norm_title(t: str) -> str:
    return "".join(str(t or "").split()).lower()


def _align(base: list[dict], other: list[dict]) -> dict[str, str]:
    """把 other 的任务对齐到 base 的任务上，返回 base_title -> other_title。"""
    used: set[int] = set()
    mapping: dict[str, str] = {}
    pool = list(other)
    for b in base:
        bt, bn = b.get("title", ""), _norm_title(b.get("title", ""))
        best, score = None, 0.0
        for i, o in enumerate(pool):
            if i in used:
                continue
            on = _norm_title(o.get("title", ""))
            if bn and on and (bn == on or bn in on or on in bn):
                cand, sc = i, 1.0
            else:
                sc = SequenceMatcher(None, bn, on).ratio() if bn and on else 0.0
                cand = i
            if sc > score:
                best, score = cand, sc
        if best is not None and score >= 0.6:
            used.add(best)
            mapping[str(bt)] = str(pool[best].get("title", ""))
    return mapping


def _cluster(titles: list[str]) -> dict[str, str]:
    """把措辞不同的同一任务聚成一个规范簇：返回 原始title -> 簇代表。"""
    reps: list[str] = []
    mapping: dict[str, str] = {}
    for t in titles:
        n = _norm_title(t)
        hit = None
        for rep in reps:
            rn = _norm_title(rep)
            if n and rn and (n == rn or n in rn or rn in n):
                hit = rep
                break
            if n and rn and SequenceMatcher(None, n, rn).ratio() >= 0.6:
                hit = rep
                break
        mapping[t] = hit if hit is not None else t
        if hit is None:
            reps.append(t)
    return mapping


def analyze(data: dict[str, Any]) -> dict[str, Any]:
    mid = data["material_id"]
    runs = data["records"]
    lists = [r["normalized_tasks"] for r in runs]

    counts = [len(x) for x in lists]
    status_rows: list[dict[str, int]] = []
    for tasks in lists:
        c = {k: 0 for k in STATUS_KEYS}
        other: dict[str, int] = {}
        for t in tasks:
            s = str(t.get("status"))
            if s in c:
                c[s] += 1
            else:
                other[s] = other.get(s, 0) + 1
        status_rows.append({**c, "其他未识别": other})

    # 跨全部 run 聚类，得到规范任务簇
    all_titles = [str(t.get("title", "")) for tasks in lists for t in tasks]
    mapping = _cluster(all_titles)

    # 每个 run 命中了哪些簇
    run_clusters: list[set[str]] = []
    for tasks in lists:
        run_clusters.append({mapping[str(t.get("title", ""))] for t in tasks})

    variants: dict[str, set[str]] = {}
    for t in all_titles:
        variants.setdefault(mapping[t], set()).add(t)

    freq = [
        {"title": rep, "appear": sum(1 for s in run_clusters if rep in s),
         "variants": sorted(variants[rep])}
        for rep in variants
    ]
    freq.sort(key=lambda x: (-x["appear"], x["title"]))

    # 字段波动（按簇对齐比较）
    field_change: dict[str, list[str]] = {}
    for rep in variants:
        seen: dict[int, dict] = {}
        for ri, tasks in enumerate(lists, start=1):
            for t in tasks:
                if mapping[str(t.get("title", ""))] == rep:
                    seen[ri] = t
        if len(seen) < 2:
            continue
        ref = seen[min(seen)]
        for ri, t in seen.items():
            if ri == min(seen):
                continue
            for f in ("status", "owner", "deadline", "department"):
                if str(ref.get(f) or "") != str(t.get(f) or ""):
                    field_change.setdefault(f"{rep} :: {f}", []).append(
                        f"run{ri}: {t.get(f)}")

    # 重点追踪
    watch: dict[str, int] = {}
    for label, keys in WATCH.get(mid, {}).items():
        for key in keys:
            hit = sum(
                1 for s in run_clusters
                if any(key in v for rep in s for v in variants[rep])
            )
            watch[f"{label}｜{key}"] = hit

    # 层级定位
    layer = {"输入不一致": False, "raw_response 一致": None, "normalize 引入差异": None}
    ih = {r["input_hash"] for r in runs}
    layer["输入不一致"] = len(ih) > 1
    raws = [json.dumps(r["raw_response"], ensure_ascii=False, sort_keys=True) for r in runs]
    if len(set(raws)) == 1:
        layer["raw_response 一致"] = True
        layer["normalize 引入差异"] = len(
            {json.dumps(l, sort_keys=True, ensure_ascii=False) for l in lists}) > 1
    else:
        layer["raw_response 一致"] = False
        layer["normalize 引入差异"] = None

    return {
        "counts": counts,
        "status_rows": status_rows,
        "freq": freq,
        "field_change": field_change,
        "watch": watch,
        "layer": layer,
        "unique_titles": len(set(all_titles)),
        "union": len(variants),
    }


# ------------------------------------------------------------------
# 报告
# ------------------------------------------------------------------
def build_report(all_data: dict[int, dict]) -> str:
    L: list[str] = []
    A = L.append
    # 以实际数据里的调用次数为准，避免 --report 独立进程时用了默认 RUNS
    total_runs = max(len(d["records"]) for d in all_data.values())
    A("# CampusFlow AI 2.0 · LLM 抽取稳定性评测报告")
    A("")
    A(f"生成时间：{datetime.now().isoformat(timespec='seconds')}")
    A(f"每份材料连续调用：{total_runs} 次　|　总 LLM 调用：{total_runs * len(all_data)} 次")
    A("")
    A("> 测量口径：每份材料**只解析一次**，5 次调用共用同一份 parsed_text 与同一份 Prompt，")
    A("> 因此下表波动**不含文件解析环节**。temperature 与 model 见下表。")
    A("")

    A("## 一、总体结果")
    A("")
    A("| 材料 | Run1 | Run2 | Run3 | Run4 | Run5 | 最小 | 最大 | 极差 | 任务簇数 | 原始措辞数 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for mid in sorted(all_data):
        d, a = all_data[mid], all_data[mid]["_a"]
        c = a["counts"]
        A(f"| 材料{mid} {d['file_name'][:22]} | " + " | ".join(str(x) for x in c)
          + f" | {min(c)} | {max(c)} | {max(c)-min(c)} | {a['union']} | {a['unique_titles']} |")
    A("")
    A("> 「任务簇数」= 按标题相似度归并后的真实任务数；「原始措辞数」= 模型实际产出的不同标题字符串数。")
    A("> 两者差距越大，说明同一任务被反复换一种说法。")
    A("")

    A("## 二、状态分布稳定性")
    A("")
    A("| 材料 | pending | processing | completed | cancelled | 其他未识别 |")
    A("|---|---|---|---|---|---|")
    for mid in sorted(all_data):
        rows = all_data[mid]["_a"]["status_rows"]
        cells = []
        for k in (*STATUS_KEYS, "其他未识别"):
            vals = [r[k] if isinstance(r[k], int) else sum(r[k].values()) for r in rows]
            rng = f"{min(vals)}–{max(vals)}" if min(vals) != max(vals) else str(vals[0])
            cells.append(rng)
        A(f"| 材料{mid} | " + " | ".join(cells) + " |")
    A("")

    A("## 三、任务级波动")
    A("")
    for mid in sorted(all_data):
        d, a = all_data[mid], all_data[mid]["_a"]
        A(f"### 材料{mid}　{d['file_name']}")
        A("")
        A(f"观察重点：{d['focus']}")
        A("")
        A("| 出现次数 | 任务标题（簇代表） | 模型实际措辞变体 |")
        A("|---:|---|---|")
        for f in a["freq"]:
            mark = ""
            if f["appear"] < total_runs and mid in WATCH:
                for label, keys in WATCH[mid].items():
                    if any(k in v for k in keys for v in f["variants"]):
                        mark = f"　← **{label}**"
            A(f"| {f['appear']}/{total_runs} | {f['title']}{mark} | {' / '.join(f['variants'])} |")
        A("")
        if a["field_change"]:
            A("字段变化：")
            A("")
            for k, v in a["field_change"].items():
                A(f"- `{k}` → {'；'.join(v)}")
        else:
            A("字段变化：无")
        A("")

    A("## 四、重点材料专项")
    A("")
    A("### 材料 8 —— 方向性误拆追踪")
    A("")
    A("原文：「下一步将抓好场地联合验收、秩序册编印、志愿者培训、开闭幕式彩排等环节」")
    A("")
    w = all_data.get(8, {}).get("_a", {}).get("watch", {})
    A("| 追踪项 | 出现次数 |")
    A("|---|---|")
    for k, v in w.items():
        A(f"| {k.split('｜')[1]} | **{v}/{total_runs}** |")
    A("")
    for mid in (7, 13):
        if mid not in all_data:
            continue
        d, a = all_data[mid], all_data[mid]["_a"]
        A(f"### 材料 {mid} —— 台账行丢失追踪（{d['file_name'][:26]}）")
        A("")
        rows = a["status_rows"]
        A("| 指标 | " + " | ".join(f"Run{i}" for i in range(1, RUNS + 1)) + " |")
        A("|---|" + "---:|" * RUNS)
        A("| 识别总数 | " + " | ".join(str(x) for x in a["counts"]) + " |")
        for k in STATUS_KEYS:
            A(f"| {k} | " + " | ".join(str(r[k]) for r in rows) + " |")
        A("")
        lost = [f"{k} = {v}/{RUNS}" for k, v in a["watch"].items() if v < total_runs]
        if lost:
            A(f"**该行/组未在全部 {total_runs} 次中出现：{'；'.join(lost)}**")
            if "已完成行" in " ".join(a["watch"].keys()):
                A("")
                A("→ 判定：**completed 行整行丢失**（不是状态识别错误）")
        else:
            A(f"**已完成行在全部 {total_runs} 次中均存在。**")
        A("")

    A("## 五、波动层级定位")
    A("")
    A("| 材料 | parsed 输入一致 | raw response 一致 | normalize 引入差异 | 判定 |")
    A("|---|---|---|---|---|")
    for mid in sorted(all_data):
        L2 = all_data[mid]["_a"]["layer"]
        if L2["输入不一致"]:
            judge = "C 文件解析链路波动"
        elif L2["raw_response 一致"] is True:
            judge = "B 后处理/normalize 波动" if L2["normalize 引入差异"] else "完全稳定"
        else:
            judge = "A LLM 原始输出波动"
        A(f"| 材料{mid} | {'否' if L2['输入不一致'] else '是'} | "
          f"{'是' if L2['raw_response 一致'] else '否（存在差异）'} | "
          f"{'-' if L2['normalize 引入差异'] is None else ('是' if L2['normalize 引入差异'] else '否')} | {judge} |")
    A("")
    A("环境：model=`%s`　base_url=`%s`　temperature=`%s`"
      % (all_data[min(all_data)]["model"], all_data[min(all_data)]["base_url"],
         all_data[min(all_data)]["temperature"]))
    A("")

    A("## 六、原始数据")
    A("")
    A("逐次原始记录（含 raw_response / parsed_items / normalized_tasks / prompt hash）见：")
    A("")
    for mid in sorted(all_data):
        A(f"- `docs/dev/_stability/raw_{mid}.json`")
    return "\n".join(L)


# ------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", nargs="*", type=int, default=[])
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--runs", type=int, default=0, help="覆盖每份材料的调用次数")
    args = ap.parse_args()

    if args.runs and args.runs > 0:
        set_runs(args.runs)

    if args.run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for mid in args.run:
            print(f"[材料{mid}] {MATERIALS[mid]['name'][:40]}", flush=True)
            data = collect(mid)
            (OUT_DIR / f"raw_{mid}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    if args.report:
        all_data: dict[int, dict] = {}
        for f in sorted(OUT_DIR.glob("raw_*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            d["_a"] = analyze(d)
            all_data[d["material_id"]] = d
        if not all_data:
            print("尚无原始数据，请先 --run", file=sys.stderr)
            return
        REPORT.write_text(build_report(all_data), encoding="utf-8")
        print(f"报告已生成：{REPORT}")


if __name__ == "__main__":
    main()
