"""v2.1 任务下发信息生成：优先复用现有 LLM，失败即回退确定性模板。

设计取向：**稳定性优先于措辞**。
    下发信息只是给负责人看的一段正式通知，它的价值在于「字段准确」，
    不在于辞藻。因此本模块对大模型的期望被压到最低——
    调不通、超时、429、返回垃圾内容，都不会阻塞下发流程，
    只会安静地退回到固定模板。绝不允许因为生成通知失败而白屏。

四条不可越过的红线（对应业务要求「不得虚构」）：
    1. 只使用任务表里已有的字段，绝不虚构负责人；
    2. 绝不虚构部门；
    3. 绝不虚构截止时间（没有就写「未设定」）；
    4. 绝不改变任务原始含义，也绝不回写 Task——本模块是**只读**的。

LLM 只被允许做一件事：把已有字段组织成更正式的中文表述。
所有字段级校验在 _normalize() 里做，不信任模型输出。
"""

from __future__ import annotations

from typing import Any, Callable

from config import settings

# 注入式调用签名，与 execution_agent.LlmFn 保持一致： (system, user) -> Any
LlmFn = Callable[[str, str], Any]

# 未指定负责人 / 截止时间时使用的占位词（明确告知，不臆造）
UNKNOWN_OWNER = "未指定负责人"
UNKNOWN_DEADLINE = "未设定"
UNKNOWN_DEPARTMENT = "未指定部门"

NOTICE_TITLE = "【工作任务通知】"

# 确定性模板：任何情况下都至少能产出这一段，字段全部来自任务表
TEMPLATE_BODY = (
    "请{owner}于{deadline}前完成{title}，并在系统中提交执行结果及相关材料。"
)


class NoticeError(RuntimeError):
    """下发信息生成异常（本模块对外只抛这一种，便于页面统一兜底）。"""


def dispatch_ready(task: dict[str, Any]) -> bool:
    """是否具备下发条件。

    **只有负责人是硬门槛**：业务上「一项任务对应一个负责人」，
    负责人为空时不得下发，也不得自行猜测。
    截止时间缺失不阻止下发，但会给出警告，由页面提示。
    """
    return bool(str(task.get("owner") or "").strip())


def missing_owner_message() -> str:
    """负责人为空时的用户提示（页面直接展示，不抛异常）。"""
    return "当前任务尚未指定负责人，请先补充负责人后再下发任务。"


def missing_deadline_message() -> str:
    """截止时间为空时的警告文案。"""
    return "该任务未设置截止时间，下发后将无法判定「已逾期未提交」，建议先补充截止时间。"


def _deadline_cn(deadline: str) -> str:
    """把 2026-10-15 转成「2026 年 10 月 15 日」；缺失时返回占位词。"""
    text = str(deadline or "").strip()
    if not text:
        return UNKNOWN_DEADLINE
    parts = text.split("-")
    if len(parts) != 3:
        return text
    try:
        year, month, day = (int(part) for part in parts)
    except ValueError:
        return text
    return f"{year} 年 {month} 月 {day} 日"


def render_template(task: dict[str, Any]) -> dict[str, str]:
    """确定性模板生成（不依赖任何外部服务）。

    输出结构固定为 title / body / fields 四段，页面直接渲染即可。
    """
    title = str(task.get("title") or "未命名事项").strip()
    owner = str(task.get("owner") or "").strip() or UNKNOWN_OWNER
    department = str(task.get("department") or "").strip() or UNKNOWN_DEPARTMENT
    deadline = str(task.get("deadline") or "").strip()

    body = TEMPLATE_BODY.format(
        owner=owner,
        deadline=_deadline_cn(deadline) if deadline else UNKNOWN_DEADLINE,
        title=title,
    )
    fields = [
        ("执行部门", department),
        ("负责人", owner),
        ("截止时间", deadline or UNKNOWN_DEADLINE),
    ]
    return {"title": NOTICE_TITLE, "body": body, "fields": fields}


def _normalize(raw: Any, task: dict[str, Any]) -> dict[str, str] | None:
    """校验并归一化模型返回；不合格返回 None，交由调用方回退模板。

    校验点：
      - 必须是 dict 且含非空 notice；
      - notice 里必须原样出现任务标题与负责人，否则视为模型改写了任务含义；
      - 负责人为空的任务本就不允许下发，这里同样拒绝。
    """
    if not isinstance(raw, dict):
        return None
    notice = str(raw.get("notice") or "").strip()
    if not notice:
        return None

    title = str(task.get("title") or "").strip()
    owner = str(task.get("owner") or "").strip()
    if title and title not in notice:
        return None
    if owner and owner not in notice:
        return None

    deadline = str(task.get("deadline") or "").strip()
    department = str(task.get("department") or "").strip()
    return {
        "title": NOTICE_TITLE,
        "body": notice,
        "fields": [
            ("执行部门", department or UNKNOWN_DEPARTMENT),
            ("负责人", owner or UNKNOWN_OWNER),
            ("截止时间", deadline or UNKNOWN_DEADLINE),
        ],
    }


SYSTEM_PROMPT = (
    "你是高校事务管理系统的文书助手。你的唯一任务是把已有的任务字段"
    "改写成一段正式、简洁的中文工作通知，不得增加任何新信息。"
)

USER_PROMPT = """请根据以下任务信息撰写一段正式的中文工作通知。

严格要求：
1. 只能使用下列字段，不得编造负责人、部门、截止时间；
2. 必须原样保留任务名称与负责人姓名，不得改写；
3. 不得添加任务之外的任何要求、时间或承诺；
4. 通知需包含明确的「请谁、在何时前、完成什么、提交什么」。

任务信息：
- 任务名称：{title}
- 执行部门：{department}
- 负责人：{owner}
- 截止时间：{deadline}
- 任务要求：{description}

只返回 JSON，格式为 {{"notice": "通知正文"}}。"""


def generate_notice(
    task: dict[str, Any],
    llm_fn: LlmFn | None = None,
) -> dict[str, Any]:
    """生成下发信息。

    流程：LLM 优先 → 任何异常或不合格结果 → 确定性模板兜底。

    返回 dict：
        title / body / fields  展示用
        source                 "llm" 或 "template"，便于界面标注实际来源
        degraded               True 表示发生过降级（LLM 不可用或不合格）

    异常：不抛异常。本模块对调用方的承诺是「一定给出一份可展示的通知」。
    """
    fallback = render_template(task)

    if not dispatch_ready(task):
        return {**fallback, "source": "template", "degraded": False}

    caller = llm_fn
    if caller is None:
        try:
            from llm import client

            caller = client.chat_json
        except Exception:
            return {**fallback, "source": "template", "degraded": True}

    prompt = USER_PROMPT.format(
        title=str(task.get("title") or ""),
        department=str(task.get("department") or "未指定"),
        owner=str(task.get("owner") or ""),
        deadline=str(task.get("deadline") or "未设定"),
        description=str(task.get("description") or "无"),
    )

    try:
        raw = caller(SYSTEM_PROMPT, prompt)
    except Exception:
        # 超时、限流、未配置 Key、网络异常……一律降级，绝不向上抛
        return {**fallback, "source": "template", "degraded": True}

    normalized = _normalize(raw, task)
    if normalized is None:
        return {**fallback, "source": "template", "degraded": True}

    return {**normalized, "source": "llm", "degraded": False}
