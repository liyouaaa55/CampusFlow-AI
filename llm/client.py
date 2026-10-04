"""DeepSeek 客户端（OpenAI 兼容协议）。

职责：
- 统一读取 config/settings.py 中的密钥、地址、模型名与超时配置；
- 提供基础对话接口 chat() 与「文本 → JSON」接口 chat_json()；
- 对模型返回做容错解析（去除 ```json 代码块围栏、前后多余解释文字）。

说明：本模块只负责「调用与解析」，不包含任何业务判断。
"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from config import settings


class LLMError(RuntimeError):
    """大模型相关异常：未配置密钥、请求失败、返回内容无法解析。"""


# 代码块围栏，如 ```json ... ``` （兼容无语言标识与多余空白）
_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)

# 缓存的客户端实例（懒加载单例，避免重复创建连接池）
_client: OpenAI | None = None


def reset_client() -> None:
    """清空缓存的客户端实例（切换密钥或编写测试时使用）。"""
    global _client
    _client = None


def get_client() -> OpenAI:
    """获取 DeepSeek 客户端；未配置 API Key 时抛出 LLMError。"""
    if not settings.is_llm_ready():
        raise LLMError(
            "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填写密钥后重试。"
        )

    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=settings.LLM_TIMEOUT,
        )
    return _client


def chat(
    messages: list[dict[str, str]],
    temperature: float | None = None,
    model: str | None = None,
) -> str:
    """基础对话调用，返回模型输出的原始文本。

    参数：
        messages      OpenAI 格式的消息列表，如 [{"role": "user", "content": "..."}]
        temperature   采样温度，默认取配置值（事项识别为 0，保证输出稳定）
        model         模型名，默认取配置值
    异常：
        LLMError      未配置密钥、请求失败或返回空内容
    """
    client = get_client()
    last_error: Exception | None = None

    # 失败按配置重试，降低网络抖动导致的失败率
    for _ in range(settings.LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model or settings.DEEPSEEK_MODEL,
                messages=messages,
                temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
            )
            content = (response.choices[0].message.content or "").strip()

            if settings.DEBUG:
                print(f"[LLM 原始返回]\n{content}\n")

            if not content:
                raise LLMError("模型返回内容为空。")
            return content
        except LLMError:
            raise
        except Exception as exc:  # 网络异常、鉴权失败、限流等
            last_error = exc

    raise LLMError(f"调用 DeepSeek 失败（已重试 {settings.LLM_MAX_RETRIES} 次）：{last_error}")


def extract_json(text: str) -> Any:
    """从模型返回文本中提取 JSON，返回 dict 或 list。

    容错顺序：
    1. 直接解析原文；
    2. 解析 ```json 代码块内的内容；
    3. 截取首个 [ 或 { 到对应结尾符号之间的片段。
    """
    if not text or not text.strip():
        raise LLMError("模型返回为空，无法解析 JSON。")

    raw = text.strip()
    candidates: list[str] = [raw]

    fence = _CODE_FENCE_RE.search(raw)
    if fence:
        candidates.insert(0, fence.group(1).strip())

    # 兜底：截取首尾括号之间的内容（应对 JSON 前后带解释文字的情况）
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise LLMError(f"模型返回内容不是合法 JSON：{raw[:200]}")


def chat_json(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
) -> Any:
    """按「System + User」组装 Prompt 调用模型，并返回解析后的 JSON 对象。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return extract_json(chat(messages, temperature=temperature))
