"""最终冲刺：F2 / F3 修复的针对性测试。

F2  通知首次生成后未立即渲染 —— 生成分支必须 rerun，使渲染分支当次生效。
F3  受限原因重复句号 —— 句末标点归一化，避免 UI 与业务服务重复追加。

两项修复都不改变业务校验与提示内容，只是渲染时机与标点归一。
"""

from __future__ import annotations

import ast
from pathlib import Path

from ui.pages import execution_panel

# render_dispatch_panel 所在文件（生成/渲染通知的页面）
_SUBMISSION_VIEW = Path(execution_panel.__file__).resolve().parent / "submission_view.py"


# ============================================================
# F3：句末标点归一
# ============================================================


def test_ensure_sentence_end_keeps_existing_period() -> None:
    assert execution_panel.ensure_sentence_end("不重复处置。") == "不重复处置。"


def test_ensure_sentence_end_appends_period() -> None:
    assert execution_panel.ensure_sentence_end("不重复处置") == "不重复处置。"


def test_ensure_sentence_end_strips_whitespace() -> None:
    assert execution_panel.ensure_sentence_end("  不重复处置。  ") == "不重复处置。"


def test_ensure_sentence_end_handles_empty() -> None:
    assert execution_panel.ensure_sentence_end("") == ""
    assert execution_panel.ensure_sentence_end(None) == ""  # type: ignore[arg-type]


def test_blocked_reason_caption_has_no_double_period() -> None:
    """受限提示拼接后不得出现连续两个句号，且保留完整原因。"""
    reason = "最近 3 天内已执行过 draft（2026-10-09 18:22:48），不重复处置。"
    caption = "限制原因：" + execution_panel.ensure_sentence_end(reason)
    assert "。。" not in caption
    assert caption.endswith("不重复处置。")
    assert "最近 3 天内已执行过" in caption


# ============================================================
# F2：生成通知后必须 rerun
# ============================================================


def test_generate_notice_branch_triggers_rerun() -> None:
    """包含 prepare_notice 的分支必须同样包含 st.rerun()。

    否则「渲染分支位于生成按钮之前」会再次导致首点不显示、需二次操作。
    """
    tree = ast.parse(_SUBMISSION_VIEW.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            dumped = ast.dump(node)
            if "prepare_notice" in dumped:
                assert "rerun" in dumped, "生成通知分支缺少 st.rerun()"
                return
    raise AssertionError("未找到包含 prepare_notice 的分支")
