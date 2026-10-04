"""轻量知识库加载器。

设计原则（与项目「不建大型 RAG」的要求一致）：
1. 知识以文件形式存在，读取后直接注入 Prompt 或作为渲染参数，不做向量检索；
2. 读取失败绝不静默产出脏数据——组织/模板类知识按「可选增强」处理，
   文件缺失时返回空结果让调用方回退到内置行为；结构非法时抛 KnowledgeError；
3. 路径统一来自 config.settings，禁止在业务代码里拼接字符串路径。

接口：
    load_departments()      读取 organization/departments.json
    organization_block()    把部门职责渲染成可注入 Prompt 的文本块（无知识时返回 ""）
    load_template_json()    读取 templates/*.json
    load_template_text()    读取 templates/*.md 等文本模板
    clear_cache()           清空缓存（测试与热更新用）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import settings

# ---------- 路径常量 ----------
# 部门职责文件：<项目根>/knowledge/organization/departments.json
ORGANIZATION_PATH: Path = settings.ORGANIZATION_DIR / "departments.json"

# 部门职责文本块的标题（注入 Prompt 时使用）
ORGANIZATION_BLOCK_HEADER: str = "【部门职责参考】"

# 注入 Prompt 时的边界声明：知识库只能帮助对齐名称，不能成为编造依据
ORGANIZATION_BLOCK_NOTICE: str = (
    "以下内容来自本校部门职责知识库，仅用于把文本中已出现的部门口径对齐到规范名称与职责范围。\n"
    "严格遵守前述规则：文本中未提及部门时仍返回 null，禁止依据本参考编造部门、负责人或时间。"
)


class KnowledgeError(RuntimeError):
    """知识库文件结构非法（文件缺失不属于错误，由调用方回退）。"""


# 模块级缓存：{路径: 解析结果}
_json_cache: dict[Path, Any] = {}


# ============================================================
# 一、部门职责
# ============================================================


def load_departments(
    path: str | Path | None = None,
    force_reload: bool = False,
) -> list[dict[str, Any]]:
    """读取部门职责列表。

    返回：[{"name": "学工办", "aliases": [...], "duties": [...]}, ...]
    文件不存在时返回空列表（知识库属于可选增强，不应阻断主流程）。
    结构非法时抛 KnowledgeError。
    """
    target = Path(path) if path is not None else ORGANIZATION_PATH

    if not target.exists():
        return []

    if force_reload:
        _json_cache.pop(target, None)

    if target not in _json_cache:
        _json_cache[target] = _read_json(target)

    payload = _json_cache[target]
    if not isinstance(payload, dict):
        raise KnowledgeError(f"部门职责文件顶层必须是对象：{target}")

    raw_departments = payload.get("departments")
    if not isinstance(raw_departments, list):
        raise KnowledgeError(f"部门职责文件缺少 departments 数组：{target}")

    departments: list[dict[str, Any]] = []
    for item in raw_departments:
        if not isinstance(item, dict):
            raise KnowledgeError(f"departments 元素必须是对象：{target}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise KnowledgeError(f"部门缺少 name 字段：{target}")
        departments.append(
            {
                "name": name,
                "aliases": _as_text_list(item.get("aliases"), target, "aliases"),
                "duties": _as_text_list(item.get("duties"), target, "duties"),
            }
        )

    return departments


def organization_block(
    departments: list[dict[str, Any]] | None = None,
    path: str | Path | None = None,
    force_reload: bool = False,
) -> str:
    """把部门职责渲染成可注入 Prompt 的文本块。

    departments 为空且磁盘上无知识文件时返回空字符串，
    调用方据此回退到不含知识的基础 Prompt。
    """
    items = departments if departments is not None else load_departments(path, force_reload)
    if not items:
        return ""

    lines: list[str] = [ORGANIZATION_BLOCK_HEADER, ORGANIZATION_BLOCK_NOTICE]
    for item in items:
        name = str(item.get("name") or "").strip()
        aliases = [str(alias) for alias in item.get("aliases") or []]
        duties = [str(duty) for duty in item.get("duties") or []]

        head = f"- {name}"
        if aliases:
            head += f"（别名：{' / '.join(aliases)}）"
        lines.append(head)
        for duty in duties:
            lines.append(f"  · {duty}")

    return "\n".join(lines)


# ============================================================
# 二、模板
# ============================================================


def load_template_json(
    name: str,
    force_reload: bool = False,
) -> dict[str, Any]:
    """读取 templates/ 下的 JSON 模板，返回字典。

    文件不存在时返回空字典（调用方回退到内置默认值）。
    内容不是对象时抛 KnowledgeError。
    """
    target = _template_path(name)
    if not target.exists():
        return {}

    if force_reload:
        _json_cache.pop(target, None)

    if target not in _json_cache:
        _json_cache[target] = _read_json(target)

    payload = _json_cache[target]
    if not isinstance(payload, dict):
        raise KnowledgeError(f"模板文件顶层必须是对象：{target}")
    return payload


def load_template_text(name: str) -> str:
    """读取 templates/ 下的文本模板（Markdown 等），返回原始文本。

    文件不存在时返回空字符串。
    """
    target = _template_path(name)
    if not target.exists():
        return ""

    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:  # 模板统一要求 UTF-8
        raise KnowledgeError(f"模板文件不是 UTF-8 编码：{target}") from exc


# ============================================================
# 三、缓存管理
# ============================================================


def clear_cache() -> None:
    """清空知识文件缓存（测试隔离与热更新使用）。"""
    _json_cache.clear()


# ============================================================
# 内部工具
# ============================================================


def _template_path(name: str) -> Path:
    """拼接模板文件路径，拒绝路径穿越。"""
    filename = Path(name).name
    if not filename:
        raise KnowledgeError("模板名称不能为空。")
    return settings.TEMPLATES_DIR / filename


def _read_json(target: Path) -> Any:
    """读取并解析 JSON，错误统一转为 KnowledgeError。"""
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KnowledgeError(f"知识库文件解析失败：{target}（{exc}）") from exc


def _as_text_list(value: Any, target: Path, field: str) -> list[str]:
    """把字段收敛为字符串列表；None/缺失返回空列表，类型错误抛 KnowledgeError。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise KnowledgeError(f"{target} 的 {field} 必须是字符串数组。")
    return [str(item).strip() for item in value if str(item).strip()]
