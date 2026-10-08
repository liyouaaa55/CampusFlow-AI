"""Evidence Rescue: recover execution facts the LLM missed (v2.0 final P0).

Very narrow by design. NOT a second TaskAgent, does no comprehension: it only
rescues a task when the source text contains an explicit execution-state fact
that the LLM failed to report.

Pipeline order:
    LLM -> Directional Guard (drop directional) -> Evidence Rescue (recover)
"""

from __future__ import annotations

from datetime import date
import re

from tools import directional_guard

# 明确「正在做」的执行状态表达（封闭清单，不做开放式语义判断）
IN_PROGRESS_MARKERS = (
    "正在实施", "正在推进", "同步推进", "进入制作阶段",
    "调整完成时间", "完成时间调整", "正在开展", "正在筹备",
)

# 明确「已做完」的表达，优先级高于上面
COMPLETED_MARKERS = (
    "已完成", "已结束", "已落实", "已建成", "已确定", "已组建",
    "已划定", "已通过", "已审核", "已批复", "已完工", "已办结",
    "已完成初稿", "已配备", "已设置",
)

# 无法督办的对象（太笼统，补出来没有意义）
VAGUE_OBJECTS = (
    "有关单位", "有关部门", "各部门", "全校", "相关部门", "相关单位",
    "四条线", "各方面", "各项", "整体",
)

# 分句分隔符：比 Directional Guard 更细——工作对象常被逗号切开，
# 例如「…,主席台搭建及音响、照明系统检修正在实施,…」
FRAGMENT_DELIMITERS = "。！？；\n，"

MIN_OBJECT_CHARS = 4
DEDUP_COVERAGE = 0.8
_YEAR_RE = re.compile(r"(\d{4})")

def split_fragments(text):
    """按句号与逗号切成片段。

    比 Directional Guard 更细：材料8 里的工作对象被逗号切开
    （「场地设施方面，跑道划线已完工，主席台搭建…正在实施」）。
    """
    buf = []
    out = []
    for ch in text or "":
        if ch in FRAGMENT_DELIMITERS:
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _marker_in(fragment):
    """返回片段中的执行状态标记；没有则返回 None。已完成态优先排除。"""
    for done in COMPLETED_MARKERS:
        if done in fragment:
            return None
    for mark in IN_PROGRESS_MARKERS:
        if mark in fragment:
            return mark
    return None


def _extract_deadline(fragment, source_text):
    """从片段中取明确日期，输出系统标准格式 YYYY-MM-DD。

    系统标准由 task_agent._DEADLINE_RE 定义，**要求 4 位年份**，
    因此本层绝不能产出 MM-DD 形态——那既不合法，也无法被 ReminderAgent 解析。

    年份只取自**原文自身**的年份证据（材料落款/成文日期），
    原文找不到年份时返回 None，绝不推断。
    """
    idx = fragment.find("月")
    if idx < 0:
        return None
    month = ""
    for ch in reversed(fragment[:idx]):
        if ch.isdigit():
            month = ch + month
        else:
            break
    day = ""
    for ch in fragment[idx + 1:]:
        if ch.isdigit():
            day += ch
        else:
            break
    if not month or not day:
        return None

    year = _year_of(source_text)
    if not year:
        return None
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _year_of(source_text):
    """取原文中的第一个 4 位年份（成文日期等），作为日期的年份依据。"""
    matched = _YEAR_RE.search(source_text or "")
    return matched.group(0) if matched else None

def _object_of(fragment, marker, previous):
    """从片段中取工作对象原文。

    两种情形：
    1. 工作对象就在标记之前：「主席台搭建及音响、照明系统检修」+「正在实施」
    2. 工作对象在上一片段：「赛场无线网络扩容因设备到货延迟」+「完成时间调整为11月11日」
    返回 None 表示取不到（不猜测）。
    """
    if marker in fragment:
        head = fragment.split(marker)[0].strip()
    else:
        head = ""
    if not head:
        head = (previous or "").strip()
        # 去掉「因…延迟 / 因…」这类原因状语，只留工作对象
        if "因" in head:
            head = head.split("因")[0].strip()
    if len(head) < MIN_OBJECT_CHARS:
        return None
    if any(v in head for v in VAGUE_OBJECTS):
        return None
    return head


def _title_for(head, marker):
    """按标记补出动作词，保持原文工作对象不拆分。"""
    if marker == "进入制作阶段":
        return head + "制作"
    return head


def _same_item(a, b):
    """确定性判重：两个标题的字符高度重叠即视为同一事项。"""
    ca = [c for c in a if not c.isspace()]
    cb = [c for c in b if not c.isspace()]
    if not ca or not cb:
        return False
    cov_ab = sum(1 for c in ca if c in b) / len(ca)
    cov_ba = sum(1 for c in cb if c in a) / len(cb)
    return max(cov_ab, cov_ba) >= DEDUP_COVERAGE

def find_candidates(source_text):
    """扫描原文，返回 Evidence Rescue 候选任务列表。

    触发条件（三者必须同时成立）：
      1. 片段含明确的「正在做」状态标记；
      2. 该片段不是方向性表述（复用 Directional Guard）；
      3. 能从当前句（或上一片段）取到明确的工作对象。
    """
    frags = split_fragments(source_text)
    out = []
    for i, frag in enumerate(frags):
        marker = _marker_in(frag)
        if not marker:
            continue
        if directional_guard.is_directional_sentence(frag):
            continue
        head = _object_of(frag, marker, frags[i - 1] if i > 0 else "")
        if not head:
            continue
        out.append({
            "title": _title_for(head, marker),
            "department": None,
            "owner": None,
            "deadline": _extract_deadline(frag, source_text),
            "priority": None,
            "status": "processing",
            "description": frag,
        })
    return out


def merge(tasks, candidates):
    """把候选补进任务列表，跳过与已有任务重复的候选。

    判重是确定性的字符重叠规则，不调用大模型。
    """
    result = list(tasks)
    for cand in candidates:
        if any(_same_item(str(t.get("title") or ""), cand["title"]) for t in result):
            continue
        result.append(dict(cand))
    return result