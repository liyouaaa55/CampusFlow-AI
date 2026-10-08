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
from database import sqlite_db
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


def render_check_window(task_id: int) -> None:
    """工作检查窗口主体。

    每次进入都重新按 task_id 读库；任务不存在直接提示并返回。
    """
    try:
        view = task_dispatch.task_check_view(task_id)
    except TaskNotFoundError:
        st.error(f"任务不存在（编号 {task_id}），无法打开工作检查。")
        return

    task = view["task"]
    submission = view.get("submission")

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
            )
            st.success(result["message"])
            st.rerun()
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


def render_dispatch_panel(
    task: Mapping[str, Any],
    task_id: int,
    scope: str = "list",
) -> None:
    """任务下发面板：生成信息 → 复制 → 系统内下发 → 进入工作检查。

    scope 只用于区分按钮 key，避免专注模式与列表模式下 key 冲突。
    """
    cards.section(TITLE_DISPATCH, "下发仅在系统内流转，不会通过外部渠道发送")

    view = task_dispatch.task_check_view(task_id)
    task = view["task"]

    if not task_dispatch.can_dispatch(task):
        # 负责人为空：禁止下发，且不猜测负责人
        cards.hint([dispatch_notice.missing_owner_message()])
        return

    if not str(task.get("deadline") or "").strip():
        st.warning(dispatch_notice.missing_deadline_message())

    _state_line(task, view)

    notice_key = f"cf_dispatch_notice_{scope}_{task_id}"
    if st.session_state.get(notice_key):
        _render_notice_card(st.session_state[notice_key])

    left, right, third = st.columns([1, 1, 1], gap="small")

    with left:
        if st.button(BTN_GENERATE, key=f"cf_dispatch_gen_{scope}_{task_id}"):
            try:
                st.session_state[notice_key] = task_dispatch.prepare_notice(task)
            except task_dispatch.OwnerRequiredError as exc:
                st.error(str(exc))
            except Exception:
                # 生成失败绝不阻塞下发：dispatch_notice 内部已保证有模板兜底，
                # 走到这里只可能是极端情况，给出提示即可，不让页面白屏
                st.error("下发信息生成失败，请稍后重试。")

    with right:
        notice = st.session_state.get(notice_key)
        if notice:
            st.button(
                BTN_COPY,
                key=f"cf_dispatch_copy_{scope}_{task_id}",
                on_click=_copy_to_clipboard,
                args=(str(notice.get("body") or ""),),
            )

    with third:
        if st.button(
            BTN_ENTER_CHECK,
            key=f"cf_dispatch_enter_{scope}_{task_id}",
            on_click=set_check_task,
            args=(task_id,),
        ):
            pass

    already = bool(view.get("dispatched"))
    if st.button(
        BTN_DISPATCH,
        key=f"cf_dispatch_do_{scope}_{task_id}",
        disabled=already,
        type="primary",
    ):
        try:
            result = task_dispatch.dispatch_task(task_id)
            (st.success if not result["already"] else st.info)(result["message"])
        except task_dispatch.OwnerRequiredError as exc:
            st.error(str(exc))
        except TaskNotFoundError:
            st.error(f"任务不存在（编号 {task_id}），下发已取消。")
        except sqlite_db.DatabaseError:
            st.error("下发写入失败，请稍后重试。")
        except Exception:
            st.error("下发失败，请稍后重试。")


def _copy_to_clipboard(text: str) -> None:
    """把下发信息写入剪贴板（失败静默，不影响页面）。"""
    try:
        st.session_state[NOTICE_CLIPBOARD_KEY] = text
    except Exception:
        pass


def set_check_task(task_id: int) -> None:
    """进入工作检查窗口：只记 task_id，绝不缓存任务内容。"""
    st.session_state[CHECK_TASK_KEY] = int(task_id)


def render_entry(
    task: Mapping[str, Any],
    task_id: int,
    scope: str = "list",
) -> None:
    """任务详情区的统一入口：按内部开关决定显示工作检查还是下发面板。"""
    active = st.session_state.get(CHECK_TASK_KEY)
    if active is not None and int(active) == int(task_id):
        render_check_window(task_id)
        if st.button(BTN_BACK, key=f"cf_check_exit_{scope}_{task_id}"):
            st.session_state.pop(CHECK_TASK_KEY, None)
            st.rerun()
        return
    render_dispatch_panel(task, task_id, scope=scope)
