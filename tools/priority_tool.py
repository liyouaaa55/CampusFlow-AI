"""优先级规则工具：读取 knowledge/rules/priority.json 并按关键词判定优先级。

设计要点：
- **不在代码中写死任何关键词**，全部从规则文件读取，便于业务人员直接维护；
- 规则文件缺失或格式错误时抛出明确异常，避免静默使用错误规则；
- 按规则文件中的级别顺序（high → medium → low）匹配，命中即返回，保证「就高不就低」。

规则文件格式（key 为优先级，value 为关键词数组）：
    {"high": ["消防", "实验室", "安全", "应急"],
     "medium": ["教学", "活动", "宣传"],
     "low": ["通知"]}
"""

from __future__ import annotations

import json
from pathlib import Path

from config import settings

# 规则文件路径：<项目根>/knowledge/rules/priority.json
PRIORITY_RULES_PATH: Path = settings.RULES_DIR / "priority.json"

# 模块级缓存：{规则文件绝对路径: 规则字典}，避免每次规划都读盘
_RULES_CACHE: dict[str, dict[str, list[str]]] = {}


class RulesError(RuntimeError):
    """规则文件缺失、格式非法或内容不合法。"""


def load_rules(path: str | Path | None = None, force_reload: bool = False) -> dict[str, list[str]]:
    """读取优先级规则。

    参数：
        path          规则文件路径，默认 knowledge/rules/priority.json
        force_reload  为 True 时忽略缓存重新读取
    返回：
        {"high": [...], "medium": [...], "low": [...]}
    异常：
        RulesError    文件不存在、JSON 非法、优先级未知或关键词格式错误
    """
    rules_path = Path(path) if path else PRIORITY_RULES_PATH
    cache_key = str(rules_path)

    if not force_reload and cache_key in _RULES_CACHE:
        return _RULES_CACHE[cache_key]

    if not rules_path.exists():
        raise RulesError(f"优先级规则文件不存在：{rules_path}")

    try:
        raw = json.loads(rules_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RulesError(f"优先级规则文件不是合法 JSON：{rules_path}（{exc}）") from exc

    if not isinstance(raw, dict):
        raise RulesError(f"优先级规则文件顶层必须是对象：{rules_path}")

    rules: dict[str, list[str]] = {}
    for level, keywords in raw.items():
        # 跳过以下划线开头的注释字段（如 _comment）
        if level.startswith("_"):
            continue
        if level not in settings.PRIORITIES:
            raise RulesError(f"规则文件存在未知优先级：{level}（允许值：{'/'.join(settings.PRIORITIES)}）")
        if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
            raise RulesError(f"优先级 {level} 的关键词必须是字符串数组：{rules_path}")
        rules[level] = [keyword.strip() for keyword in keywords if keyword.strip()]

    if not rules:
        raise RulesError(f"优先级规则文件内容为空：{rules_path}")

    _RULES_CACHE[cache_key] = rules
    return rules


def clear_cache() -> None:
    """清空规则缓存（修改规则文件或编写测试时使用）。"""
    _RULES_CACHE.clear()


def matched_keywords(
    text: str,
    rules: dict[str, list[str]] | None = None,
) -> list[tuple[str, str]]:
    """返回文本中命中的关键词列表，元素为 (优先级, 关键词)。

    按规则文件中的级别顺序输出，因此第一个元素即最高命中级别。
    匹配不区分大小写，采用「包含」判断（中文无需分词）。
    """
    if not text:
        return []

    active_rules = rules if rules is not None else load_rules()
    normalized = str(text).lower()

    hits: list[tuple[str, str]] = []
    for level, keywords in active_rules.items():
        for keyword in keywords:
            if keyword.lower() in normalized:
                hits.append((level, keyword))
    return hits


def score_by_keywords(
    text: str,
    rules: dict[str, list[str]] | None = None,
) -> str:
    """按关键词规则返回优先级。

    命中多个级别时取规则文件中靠前的级别（high → medium → low，就高不就低）；
    完全未命中时返回 settings.DEFAULT_PRIORITY（默认 medium）。
    """
    hits = matched_keywords(text, rules=rules)
    if hits:
        return hits[0][0]
    return settings.DEFAULT_PRIORITY
