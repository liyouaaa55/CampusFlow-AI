"""第一阶段框架自检测试。

验证内容：
1. 配置模块可正常导入，关键配置项存在；
2. 目录结构完整（knowledge / database / llm / agent / tools / ui / docs）；
3. 运行时目录会被自动创建；
4. 枚举取值符合设计文档（状态 6 种、优先级 3 级）；
5. 密钥脱敏函数行为正确。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import settings

# 设计文档要求的顶层目录
EXPECTED_DIRS = ["config", "llm", "agent", "tools", "database", "knowledge", "ui", "tests", "docs"]


def test_base_dir_is_project_root() -> None:
    """项目根目录下应能同时看到 app.py 与 config/ 目录。"""
    assert (settings.BASE_DIR / "app.py").exists()
    assert (settings.BASE_DIR / "config").is_dir()


@pytest.mark.parametrize("name", EXPECTED_DIRS)
def test_expected_directory_exists(name: str) -> None:
    """逐个校验项目目录是否存在。"""
    assert (settings.BASE_DIR / name).is_dir()


def test_runtime_dirs_created() -> None:
    """data/uploads 等运行时目录应由配置模块自动创建。"""
    assert settings.DATA_DIR.is_dir()
    assert settings.UPLOAD_DIR.is_dir()
    assert settings.ORGANIZATION_DIR.is_dir()
    assert settings.RULES_DIR.is_dir()
    assert settings.TEMPLATES_DIR.is_dir()


def test_statuses_match_spec() -> None:
    """任务状态必须恰好为设计文档规定的 6 种。"""
    assert settings.TASK_STATUSES == (
        "pending",
        "confirmed",
        "processing",
        "urgent",
        "completed",
        "overdue",
    )
    # 中文标签需覆盖全部状态
    assert set(settings.STATUS_LABELS) == set(settings.TASK_STATUSES)


def test_priorities_and_defaults() -> None:
    """优先级为三级，且默认值与状态默认值符合约定。"""
    assert settings.PRIORITIES == ("high", "medium", "low")
    assert settings.DEFAULT_STATUS == "pending"
    assert settings.DEFAULT_PRIORITY in settings.PRIORITIES


def test_reminder_thresholds() -> None:
    """提醒阈值：紧急 1 天、临期 3 天，且紧急阈值不大于临期阈值。"""
    assert settings.REMINDER_URGENT_DAYS == 1
    assert settings.REMINDER_DUE_SOON_DAYS == 3
    assert settings.REMINDER_URGENT_DAYS <= settings.REMINDER_DUE_SOON_DAYS


def test_mask_secret() -> None:
    """密钥脱敏不得泄露完整内容。"""
    assert settings.mask_secret("") == "（未配置）"
    key = "sk-1234567890abcdef"
    masked = settings.mask_secret(key)
    assert key not in masked
    assert masked.startswith("sk-1") and masked.endswith("cdef")


def test_db_path_under_data_dir() -> None:
    """数据库文件必须位于 data 目录内。"""
    assert settings.DB_PATH.parent == settings.DATA_DIR
    assert Path(settings.DB_PATH).suffix == ".db"
