"""v2.1 任务下发面板与工作检查窗口（作为任务详情内部的「工作窗口」呈现）。

为什么不做成第六个一级导航：
    2.0 的五个主页面与其导航结构已被测试锁定（test_app_smoke 逐值断言页面列表）。
    而「工作检查」本质是某个任务的二级视图，离开任务上下文它没有意义。
    因此这里用 state 里的内部开关在同一页面内切换，页面数量与名称保持不变。

防串任务是本模块的第一要务：
    任何渲染之前都先 `view = task_dispatch.task_check_view(task_id)`，
    即**按当前 task_id 重新读库**，绝不复用 session_state 里的旧快照。
    任务不存在时直接给出中文提示并中止，绝不显示上一个任务的内容。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import streamlit as st

from config import settings
from database import models, sqlite_db, task_metadata_store
from database.sqlite_db import TaskNotFoundError
from tools import dispatch_notice, task_dispatch
from ui import labels
from ui.components import cards

# 附件存放目录（data/ 已被 .gitignore 覆盖，无需额外规则）
SUBMISSION_DIR: Path = settings.DATA_DIR / "submissions"

# 沿用上传页的大小上限，保持全站一致
MAX_ATTACH_MB: int = 20
ALLOWED_ATTACH_TYPES: list[str] = ["pdf", "docx", "txt", "md", "xlsx", "xls", "csv", "png", "jpg"]

# --- 全部用户可见文案（集中在此，避免英文枚举泄漏到界面）---
TITLE_DISPATCH = "任务下发"
TITLE_CHECK = "工作检查"
BTN_GENERATE = "生成下发信息"
BTN_COPY = "复制下发信息"
BTN_DISPATCH = "系统内下发"
BTN_ENTER_CHECK = "进入工作检查"
BTN_SUBMIT = "提交工作回执"
BTN_BACK = "返回任务详情"

LBL_STATE = "执行状态"
LBL_OWNER = "负责人"
LBL_DEPT = "执行部门"
LBL_DEADLINE = "截止时间"
LBL_REQUIREMENT = "任务要求"
LBL_CONTENT = "完成情况"
LBL_ATTACH = "附件"
LBL_SUBMITTED_AT = "提交时间"
LBL_NOT_DISPATCHED = "未下发"

# 下发前分析判定「暂不具备下发条件」时的提示（由 tasks_page 传入 block_dispatch 触发）
BLOCK_DISPATCH_HINT: str = "下发前分析发现关键执行信息缺失，已阻止直接下发（未发送任何通知）。"

STATE_COLORS: dict[str, str] = {
    "pending_submit": "orange",
    "submitted": "green",
    "overdue_unsubmitted": "red",
}

# --- session_state 键 ---
#
# 只存 task_id，**绝不存任务内容快照**。
# 这是「防串任务」的关键：任务数据每次都按 id 重新读库，
# 因此切换任务后不可能显示上一个任务的内容。
CHECK_TASK_KEY: str = "cf_check_task_id"
NOTICE_CLIPBOARD_KEY: str = "cf_dispatch_notice_clipboard"


def _text(task: Mapping[str, Any], key: str) -> str:
    """取任务字段并给占位，绝不显示 None。"""
    return str(task.get(key) or "").strip() or labels.UNKNOWN_TEXT


def save_attachment(uploaded: Any) -> Path | None:
    """保存回执附件，返回落盘路径；失败返回 None（绝不抛异常拖垮页面）。

    安全处理与上传页保持一致：
    - Path(uploaded.name).name 只取文件名，杜绝 ../../ 路径穿越；
    - 时间戳前缀避免同名覆盖；
    - 先校验大小再写盘。
    """
    try:
        if uploaded is None:
            return None
        if int(uploaded.size) > MAX_ATTACH_MB * 1024 * 1024:
            return None
        SUBMISSION_DIR.mkdir(parents=True, exist_ok=True)
        filename = Path(uploaded.name).name
        if not filename:
            return None
        target = SUBMISSION_DIR / f"{sqlite_db.now_iso()[:19].replace(':', '')}_{filename}"
        target.write_bytes(uploaded.getbuffer())
        return target
    except Exception:
        # 附件不是必填项，保存失败绝不导致整个页面崩溃
        return None


def _state_line(task: Mapping[str, Any], view: Mapping[str, Any]) -> None:
    """渲染执行回执状态一行（已提交时附带提交时间，未提交时附带截止时间）。"""
    state = str(view.get("state") or "")
    color = STATE_COLORS.get(state, "gray")
    cards.badge_row(
        [
            (LBL_STATE, labels.submission_label(state), color),
            (
                LBL_SUBMITTED_AT,
                str((view.get("submission") or {}).get("submitted_at") or labels.UNKNOWN_TEXT),
                "gray",
            ),
        ]
    )


def render_check_window(task_id: int, *, compact: bool = False) -> None:
    """工作检查窗口主体。

    每次进入都重新按 task_id 读库；任务不存在直接提示并返回。

    compact=True（执行检查页使用）：任务名称 / 责任部门 / 负责人 / 截止时间
    已由所在页面的顶部任务卡承载，这里不再重复渲染标题与那行字段，
    只保留回执状态、办理内容、附件与提交入口，避免同一屏重复堆叠任务字段。
    默认 compact=False 时行为与改造前完全一致（任务督办中心的工作检查入口）。
    """
    try:
        view = task_dispatch.task_check_view(task_id)
    except TaskNotFoundError:
        st.error(f"任务不存在（编号 {task_id}），无法打开工作检查。")
        return

    task = view["task"]
    submission = view.get("submission")

    if not compact:
        cards.section(TITLE_CHECK, "负责人进入本窗口提交执行回执，系统据此判定提交状态")

        st.markdown(f"**{_text(task, 'title')}**")
        st.caption(
            f"{LBL_DEPT}：{_text(task, 'department')}　｜　"
            f"{LBL_OWNER}：{_text(task, 'owner')}　｜　"
            f"{LBL_DEADLINE}：{_text(task, 'deadline')}"
        )

    if view.get("dispatched"):
        cards.hint(["该任务已在系统内下发。"])
    else:
        cards.hint(["该任务尚未下发，可先在任务详情中完成系统内下发。"])

    _state_line(task, view)

    if submission:
        st.markdown(f"**{LBL_CONTENT}**")
        st.write(str(submission.get("content") or ""))
        attachment = str(submission.get("file_path") or "").strip()
        if attachment:
            st.caption(f"{LBL_ATTACH}：{attachment}")
        st.write("")
        # 返回按钮由 render_entry 统一提供，此处不再重复渲染
        return

    if not view.get("dispatched"):
        st.warning(
            "该任务尚未在系统内下发，无法提交办理回执。"
            "请先在任务详情完成系统内下发，再进入本窗口提交结果。"
        )
        return

    with st.form(f"cf_submit_form_{task_id}"):
        content = st.text_area(
            LBL_CONTENT,
            height=140,
            key=f"cf_check_content_{task_id}",
            placeholder="请填写本事项的实际完成情况与结果。",
        )
        uploaded = st.file_uploader(
            LBL_ATTACH,
            type=ALLOWED_ATTACH_TYPES,
            key=f"cf_check_file_{task_id}",
            help=f"附件为可选项，单个文件不得超过 {MAX_ATTACH_MB}MB。",
        )
        submitted = st.form_submit_button(BTN_SUBMIT, type="primary")

    if submitted:
        try:
            attachment_path = save_attachment(uploaded)
            result = task_dispatch.submit_work(
                task_id=task_id,
                content=content,
                file_path=attachment_path,
                require_dispatched=True,
            )
            st.success(result["message"])
            st.rerun()
        except task_dispatch.NotDispatchedError as exc:
            st.error(str(exc))
        except task_dispatch.ContentRequiredError as exc:
            st.error(str(exc))
        except TaskNotFoundError:
            st.error(f"任务不存在（编号 {task_id}），提交已取消。")
        except Exception:
            # 数据库写入失败：给出明确中文提示，且不产生半成品状态
            st.error("回执保存失败，请稍后重试。若持续失败请联系管理员。")


# ============================================================
# 下发面板
# ============================================================


def _render_notice_card(notice: Mapping[str, Any]) -> None:
    """渲染正式的下发信息卡片。"""
    cards.leader_summary([str(notice.get("body") or "")], title=str(notice.get("title") or ""))
    rows = [(str(name), str(value)) for name, value in (notice.get("fields") or [])]
    if rows:
        cards.side_card("下发信息", "字段全部取自任务表，未作任何补充或推断", rows)


def _dispatch_record(task_id: int) -> dict[str, Any] | None:
    """取系统内下发的日志记录（首次下发）。

    只读：从 task_logs 中 action=dispatch 的存在性推导，与
    submission_check.is_dispatched 同源，因此不可能出现「状态与实际不符」。
    用途仅在展示真实的下发时间，不派生任何新的业务状态。
    """
    try:
        for log in sqlite_db.get_logs(int(task_id)):
            if log.get("action") == models.ACTION_DISPATCH:
                return dict(log)
    except Exception:
        return None
    return None


def _prerequisite_rows(task: Mapping[str, Any], task_id: int) -> list[tuple[str, bool, str]]:
    """下发前置条件 → [(条件名, 是否满足, 说明)]。

    只读展示，判断口径与 task_dispatch.dispatch_block_reason 及
    task_metadata_store 保持一致，不新增业务规则。
    """
    owner = str(task.get("owner") or "").strip()
    confirmed = str(task.get("status") or "") == "confirmed" or not bool(
        task.get("need_confirm")
    )
    meta = task_metadata_store.get_metadata(int(task_id))
    criteria = list(meta.get("closure_criteria") or [])
    if criteria and meta.get("closure_confirmed"):
        criteria_ok, criteria_text = True, "已确认"
    elif criteria:
        criteria_ok, criteria_text = False, "已设置、未确认"
    else:
        criteria_ok, criteria_text = False, "尚未设置"

    return [
        ("任务确认", confirmed, "已确认" if confirmed else "待人工确认"),
        ("负责人", bool(owner), owner or "未指定（未指定负责人不得下发）"),
        ("办结标准", criteria_ok, criteria_text),
    ]


def _render_dispatch_prerequisites(task: Mapping[str, Any], task_id: int) -> None:
    """下发前置条件：逐条列出满足情况，让「为什么不能下发」一眼可见。"""
    st.markdown("**下发前置条件**")
    for name, ok, text in _prerequisite_rows(task, task_id):
        st.markdown(f"- {name}：{text}（{'满足' if ok else '未满足'}）")


def render_dispatch_panel(
    task: Mapping[str, Any],
    task_id: int,
    scope: str = "list",
    block_dispatch: bool = False,
) -> None:
    """任务下发面板：生成信息 → 复制 → 系统内下发 → 进入工作检查。

    scope 只用于区分按钮 key，避免专注模式与列表模式下 key 冲突。
    block_dispatch=True（由下发前分析结论驱动）时，拒绝进入下发操作，
    要求管理者先补齐关键执行信息——避免「带病下发」。
    """
    cards.section(TITLE_DISPATCH, "下发仅在系统内流转，不会通过外部渠道发送")

    view = task_dispatch.task_check_view(task_id)
    task = view["task"]

    _render_dispatch_prerequisites(task, task_id)

    reason_block = task_dispatch.dispatch_block_reason(task)
    if reason_block == "owner":
        # 负责人为空：禁止下发，且不猜测负责人
        cards.hint(
            [
                dispatch_notice.missing_owner_message(),
                "下一步：请先补齐该任务的负责人（负责人是下发的硬门槛）。",
            ]
        )
        return
    if reason_block == "need_confirm":
        # 需要人工确认但尚未确认：禁止生成与下发，避免把未确认任务当正式内容
        cards.hint(
            [
                task_dispatch.unconfirmed_message(),
                "下一步：请先在上方完成「任务确认」，再回到此处下发。",
            ]
        )
        return

    if block_dispatch:
        # 下发前分析已判定「暂不具备下发条件」：不再提供模拟下发入口，
        # 引导管理者回到「任务信息修正」补齐后重新分析。
        cards.hint(
            [
                BLOCK_DISPATCH_HINT,
                "下一步：请在上方「任务信息修正」补齐关键执行信息，然后重新进行「下发前智能分析」。",
            ]
        )
        return

    if not str(task.get("deadline") or "").strip():
        st.warning(dispatch_notice.missing_deadline_message())

    _state_line(task, view)

    # 已下发：直接展示真实的下发状态与时间，不再摆放不可用的下发按钮。
    # 「系统内下发」只是本系统内的状态推进，绝不等于任何外部消息已送达。
    if view.get("dispatched"):
        record = _dispatch_record(task_id)
        dispatched_at = str((record or {}).get("created_at") or labels.UNKNOWN_TEXT)
        cards.hint(
            [
                f"该任务已在系统内下发（下发时间：{dispatched_at}）。",
                "无需重复下发；可点击「进入工作检查」查看或提交办理结果。",
                "「系统内下发」仅代表本系统内的状态推进，不代表外部消息已送达。",
            ]
        )
        _render_notice_actions(task, task_id, scope=scope)
        return

    notice_key = f"cf_dispatch_notice_{scope}_{task_id}"
    notice = st.session_state.get(notice_key)
    if notice:
        _render_notice_card(notice)
        # 提供可手动复制的纯文本：当前环境无法自动写入系统剪贴板，
        # 不假装复制成功，由用户通过文本块右上角复制按钮手动复制。
        st.code(str(notice.get("body") or ""), language="text")
        st.caption("生成通知与复制通知都不会改变任务状态；只有点击下方「系统内下发」才推进系统内状态。")

    _render_notice_actions(task, task_id, scope=scope)

    if st.button(
        BTN_DISPATCH,
        key=f"cf_dispatch_do_{scope}_{task_id}",
        type="primary",
    ):
        try:
            result = task_dispatch.dispatch_task(task_id)
            (st.success if not result["already"] else st.info)(result["message"])
        except task_dispatch.OwnerRequiredError as exc:
            st.error(str(exc))
        except task_dispatch.NotReadyError as exc:
            st.error(str(exc))
        except TaskNotFoundError:
            st.error(f"任务不存在（编号 {task_id}），下发已取消。")
        except sqlite_db.DatabaseError:
            st.error("下发写入失败，请稍后重试。")
        except Exception:
            st.error("下发失败，请稍后重试。")


def _render_notice_actions(task: Mapping[str, Any], task_id: int, scope: str) -> None:
    """生成通知 → 复制通知 → 进入工作检查（三列，保持既有交互与 key 规则）。"""
    notice_key = f"cf_dispatch_notice_{scope}_{task_id}"
    left, right, third = st.columns([1, 1, 1], gap="small")

    with left:
        if st.button(BTN_GENERATE, key=f"cf_dispatch_gen_{scope}_{task_id}"):
            try:
                meta = task_metadata_store.get_metadata(int(task_id))
                st.session_state[notice_key] = task_dispatch.prepare_notice(
                    task, metadata=meta
                )
            except task_dispatch.OwnerRequiredError as exc:
                st.error(str(exc))
            except task_dispatch.NotReadyError as exc:
                st.error(str(exc))
            except Exception:
                # 生成失败绝不阻塞下发：dispatch_notice 内部已保证有模板兜底，
                # 走到这里只可能是极端情况，给出提示即可，不让页面白屏
                st.error("下发信息生成失败，请稍后重试。")
            else:
                # 通知已写入 session_state；rerun 一次，让上方「渲染分支」
                # （位于按钮之前）立即读到新值并展示卡片，解决「首点不显示、
                # 需再次操作」的问题。rerun 不重复生成，也不改动任务与下发记录。
                st.rerun()

    with right:
        notice = st.session_state.get(notice_key)
        if notice:
            st.button(
                BTN_COPY,
                key=f"cf_dispatch_copy_{scope}_{task_id}",
                on_click=_copy_to_clipboard,
                args=(str(notice.get("body") or ""),),
            )
            if st.session_state.get("cf_copy_hint"):
                st.info(
                    "当前环境无法自动复制到剪贴板，请使用上方文本块右上角的复制按钮手动复制通知内容。"
                )

    with third:
        if st.button(
            BTN_ENTER_CHECK,
            key=f"cf_dispatch_enter_{scope}_{task_id}",
            on_click=set_check_task,
            args=(task_id,),
        ):
            pass


def _copy_to_clipboard(text: str) -> None:
    """标记用户点击过「复制下发信息」。

    当前运行环境无法把文本自动写入操作系统剪贴板，因此不假装复制成功；
    真正可用的复制由通知下方的 st.code 文本块（带浏览器复制按钮）承担，
    用户手动复制即可。
    """
    st.session_state["cf_copy_hint"] = True


def set_check_task(task_id: int) -> None:
    """进入工作检查窗口：只记 task_id，绝不缓存任务内容。"""
    st.session_state[CHECK_TASK_KEY] = int(task_id)


def render_entry(
    task: Mapping[str, Any],
    task_id: int,
    scope: str = "list",
    block_dispatch: bool = False,
) -> None:
    """任务详情区的统一入口：按内部开关决定显示工作检查还是下发面板。"""
    active = st.session_state.get(CHECK_TASK_KEY)
    if active is not None and int(active) == int(task_id):
        render_check_window(task_id)
        if st.button(BTN_BACK, key=f"cf_check_exit_{scope}_{task_id}"):
            st.session_state.pop(CHECK_TASK_KEY, None)
            st.rerun()
        return
    render_dispatch_panel(task, task_id, scope=scope, block_dispatch=block_dispatch)
