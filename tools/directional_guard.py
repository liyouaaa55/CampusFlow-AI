"""Directional guard for v2.0 final P0 fix.

Deterministic source-evidence check applied after LLM extraction.
Solves: (A) splitting future-work overview into formal tasks,
(B) hallucinating department/owner from unrelated paragraphs.
"""

from __future__ import annotations

# 未来工作概述的时间/语气标记
DIRECTIONAL_MARKERS = (
    "下一步", "下一阶段", "后续", "今后", "持续", "着力", "进一步", "在此基础上",
)

# 并列枚举标记：后面跟的是一串「方面」而不是一条指令
ENUMERATION_MARKERS = ("等环节", "等方面", "等工作", "等事项", "等各项", "等各项工作")

# 具体执行指令标记：出现即自带责任关系或时限，不属于方向性概述
CONCRETE_ASSIGNMENT = (
    "负责", "牵头", "须于", "需于", "应于", "前完成", "前报送", "前提交",
    "前落实", "前举办", "前开展", "抓紧", "务必",
)

# 标题常见前置动作词，比较时剥掉，只留事项本体
LEADING_VERBS = (
    "开展", "组织", "推进", "完成", "落实", "举办", "实施", "编印", "安排",
    "做好", "加强", "着手", "负责", "切实", "筹备", "准备", "推行", "进行",
    "开展好", "统一", "集中", "专项", "全面",
)

SENTENCE_DELIMITERS = "。！？；\n"
MIN_CORE_CHARS = 3
CORE_COVERAGE = 0.9


def split_sentences(text):
    """按句号类终止符切句。

    刻意不按逗号切：「下一步将抓好A、B、C等环节，确保……」是一句话，
    按逗号切会把枚举项和指令语义打散。
    """
    buf = []
    out = []
    for ch in text or "":
        if ch in SENTENCE_DELIMITERS:
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


def title_core(title):
    """取标题本体：剥离前置动作词后的核心事项文字。"""
    core = (title or "").strip()
    changed = True
    while changed:
        changed = False
        for verb in LEADING_VERBS:
            if core.startswith(verb) and len(core) > len(verb):
                core = core[len(verb):]
                changed = True
                break
    return core.strip() or (title or "").strip()


def _core_in_sentence(core, sentence):
    """标题本体是否在该句中出现。

    用字符覆盖率而非子串匹配：标题「编印秩序册」与原句「秩序册编印」
    语序不同但指的是同一件事，子串匹配会漏判。
    """
    chars = [c for c in core if not c.isspace()]
    if len(chars) < MIN_CORE_CHARS:
        return False
    hit = sum(1 for c in chars if c in sentence)
    return hit / len(chars) >= CORE_COVERAGE


def is_directional_sentence(sentence):
    """判断一句话是「未来工作概述」还是「具体执行指令」。

    三条同时成立才算方向性概述：含方向标记、含枚举标记或三项以上并列、
    且**不含**任何具体执行指令标记。任一不成立即视为具体指令。
    """
    if any(m in sentence for m in CONCRETE_ASSIGNMENT):
        return False
    if not any(m in sentence for m in DIRECTIONAL_MARKERS):
        return False
    if any(m in sentence for m in ENUMERATION_MARKERS):
        return True
    return sentence.count("、") >= 2


def classify(title, source_text):
    """判断标题在原文中的出处类型，并给出责任字段的取证句。

    返回键：
        kind         no_evidence / explicit / directional
        directional  True 表示应剔除
        evidence     该事项对应的原文句子（责任字段取证用）
    """
    sentences = split_sentences(source_text)
    core = title_core(title)
    chars = [c for c in core if not c.isspace()]
    if len(chars) < MIN_CORE_CHARS:
        return {"kind": "no_evidence", "directional": False, "evidence": ""}

    exact = [s for s in sentences if all(c in s for c in chars)]
    if not exact:
        return {"kind": "no_evidence", "directional": False, "evidence": ""}

    for s in exact:
        if is_directional_sentence(s):
            return {"kind": "directional", "directional": True, "evidence": s}

    return {"kind": "explicit", "directional": False, "evidence": exact[0]}


def has_field_evidence(evidence, field):
    """责任字段是否在该事项所属的那一句里出现。

    只认同句证据。模型若把**其他段落**的部门填到本事项上，这里会判为无依据，
    从而阻断「跨段落责任转移式补全」。
    """
    value = "".join((field or "").split())
    if not value or not evidence:
        return False
    return value in "".join(evidence.split())