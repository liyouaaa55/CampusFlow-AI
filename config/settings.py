"""CampusFlow AI 全局配置中心。

约定：
1. 所有可调参数集中在本文件，业务代码只从这里读取，禁止硬编码。
2. 敏感信息（API Key）按「Streamlit Secrets > .env > 默认值」三级顺序读取，
   绝不写死在代码里，也绝不读取项目根目录之外的配置文件。
3. 本文件只做「配置声明」，不包含任何业务逻辑。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

# ============================================================
# 一、目录与路径
# ============================================================

# 项目根目录（本文件位于 <root>/config/settings.py）
BASE_DIR: Path = Path(__file__).resolve().parent.parent

# 知识库（轻量，非 RAG，直接读文件注入 Prompt）
KNOWLEDGE_DIR: Path = BASE_DIR / "knowledge"
ORGANIZATION_DIR: Path = KNOWLEDGE_DIR / "organization"  # 部门职责
RULES_DIR: Path = KNOWLEDGE_DIR / "rules"                # 优先级 / 提醒规则
TEMPLATES_DIR: Path = KNOWLEDGE_DIR / "templates"        # 会议纪要 / 任务 / 报告模板

# 运行时数据目录（已被 .gitignore 忽略）
DATA_DIR: Path = BASE_DIR / "data"
UPLOAD_DIR: Path = DATA_DIR / "uploads"                  # 上传的原始文件
DB_PATH: Path = DATA_DIR / "campusflow.db"               # SQLite 数据库文件

# 需要保证存在的目录列表，供 ensure_directories() 使用
REQUIRED_DIRS: tuple[Path, ...] = (
    DATA_DIR,
    UPLOAD_DIR,
    ORGANIZATION_DIR,
    RULES_DIR,
    TEMPLATES_DIR,
)

# ============================================================
# 二、应用元信息
# ============================================================

APP_NAME: str = "CampusFlow AI"
APP_SUBTITLE: str = "高校基层事务闭环执行智能体"
APP_VERSION: str = "1.0.0"

# 演示数据标识：所有模拟数据的地方都必须展示该标签
DEMO_DATA_LABEL: str = "演示数据（Synthetic Data）"

# ============================================================
# 三、大模型配置（OpenAI 兼容协议：硅基流动 / DeepSeek 等）
# ============================================================

# 环境变量文件：只读「项目根目录下」这一个固定路径。
# 刻意不使用 find_dotenv()，避免向上级目录搜寻、误读到项目之外的文件。
ENV_FILE: Path = BASE_DIR / ".env"
load_dotenv(ENV_FILE)


# Streamlit 的标准 secrets 位置（无法从运行时取得路径列表时用作兜底）
_SECRETS_FALLBACK_PATHS: tuple[Path, ...] = (
    Path.home() / ".streamlit" / "secrets.toml",
    BASE_DIR / ".streamlit" / "secrets.toml",
)


def _streamlit_secrets_available() -> bool:
    """判断当前是否存在可用的 Streamlit Secrets 文件。

    为什么必须先判断文件：
    在未创建 secrets.toml 时访问 st.secrets[key]，Streamlit 会向页面渲染一条
    红色错误提示（"No secrets files found. Valid paths ..."），污染界面。
    因此这里先确认文件确实存在，再决定是否读取；不存在就直接回退到 .env。

    路径优先取 Streamlit 运行时自己的列表（私有属性，兼容性最好），
    取不到时退回标准位置猜测。
    """
    try:
        import streamlit as st
        from streamlit import runtime
    except Exception:  # streamlit 未安装
        return False

    try:
        if not runtime.exists():  # 非 Streamlit 运行环境（pytest / 脚本导入）
            return False
    except Exception:
        return False

    try:
        declared = getattr(st.secrets, "_file_paths", None)
    except Exception:
        declared = None

    candidates = [Path(str(item)) for item in declared] if declared else list(_SECRETS_FALLBACK_PATHS)
    return any(candidate.is_file() for candidate in candidates)


def _read_streamlit_secret(key: str) -> str:
    """从 Streamlit Secrets 读取配置（Streamlit Cloud 部署时使用）。

    仅在「运行在 Streamlit 运行时内」且「secrets 文件确实存在」时读取，其余情况
    一律返回空串，交由 .env / 默认值兜底：

    - 本地 pytest 或脚本直接导入 config：无运行时 → 跳过，不抛异常也不产生日志；
    - 本地 streamlit run 但未创建 secrets.toml：文件不存在 → 跳过，
      并且**不会**在页面上出现红色报错；
    - 云端已配置 Secrets：正常读取。
    """
    if not _streamlit_secrets_available():
        return ""

    try:
        import streamlit as st

        return str(st.secrets[key]).strip()
    except Exception:  # 键不存在等异常情况
        return ""


def read_config(key: str, default: str = "") -> str:
    """按优先级读取配置：Streamlit Secrets > .env（环境变量）> 默认值。

    这样同一份代码既能跑在本地（读 .env），也能直接部署到
    Streamlit Cloud（读 Settings → Secrets），无需改代码。
    """
    for value in (_read_streamlit_secret(key), os.getenv(key, "").strip()):
        if value:
            return value
    return default


# 延迟读取的配置项：键 → 默认值
_LAZY_CONFIG: dict[str, str] = {
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DEEPSEEK_MODEL": "deepseek-chat",
}

# 供类型检查器识别（运行时不执行，因此模块命名空间中并不存在这三个名字，
# 首次访问时由下方的模块级 __getattr__ 真正读取并缓存）
if TYPE_CHECKING:
    DEEPSEEK_API_KEY: str
    DEEPSEEK_BASE_URL: str
    DEEPSEEK_MODEL: str


def __getattr__(name: str) -> str:
    """PEP 562 模块级延迟属性：首次访问时才真正读取配置。

    为什么必须延迟读取：
    Streamlit 要求 set_page_config() 必须是本轮运行的**第一个** Streamlit 命令。
    而读取 Streamlit Secrets 需要访问 st.secrets，该访问在缺少 secrets 文件时
    会产生 Streamlit 提示消息。若在模块导入阶段（app.py 顶部的 import）就去读，
    提示消息会先于 set_page_config() 产生，导致应用启动报错。

    改为「首次访问时才读」后，读取必然发生在页面初始化之后，本地与云端都安全。
    """
    if name not in _LAZY_CONFIG:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = read_config(name, _LAZY_CONFIG[name])
    globals()[name] = value  # 缓存为真实模块属性，后续访问不再走这里
    return value


# 事项识别属于结构化抽取任务，温度固定为 0 以保证结果稳定
LLM_TEMPERATURE: float = 0.0
LLM_TIMEOUT: int = 60          # 单次请求超时（秒）
LLM_MAX_RETRIES: int = 2       # 失败重试次数

# 调试开关：为 1 时打印 LLM 原始返回（只读环境变量，不涉及 Secrets）
DEBUG: bool = os.getenv("CAMPUSFLOW_DEBUG", "0").strip() == "1"

# ============================================================
# 四、业务枚举（数据库与界面统一使用，避免各处写字符串）
# ============================================================

# 任务状态：仅允许以下 6 种
# pending   待办（识别后默认状态）
# confirmed 人工已确认
# processing 进行中
# urgent    临期/紧急（由截止时间推导）
# completed 已完成
# overdue   已逾期（由截止时间推导）
TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "confirmed",
    "processing",
    "urgent",
    "completed",
    "overdue",
)

# 状态中文名，用于界面展示
STATUS_LABELS: dict[str, str] = {
    "pending": "待办",
    "confirmed": "已确认",
    "processing": "进行中",
    "urgent": "临期",
    "completed": "已完成",
    "overdue": "已逾期",
}

# 优先级：仅允许三级
PRIORITIES: tuple[str, ...] = ("high", "medium", "low")
PRIORITY_LABELS: dict[str, str] = {"high": "高", "medium": "中", "low": "低"}

# 新识别任务的默认状态与默认优先级（在 PlannerAgent 中补齐）
DEFAULT_STATUS: str = "pending"
DEFAULT_PRIORITY: str = "medium"

# 数据来源类型，用于 tasks.source_type 字段
SOURCE_TYPES: tuple[str, ...] = ("meeting", "document", "excel", "text", "manual")

# ============================================================
# 五、提醒阈值（ReminderAgent 使用）
# ============================================================

REMINDER_URGENT_DAYS: int = 1      # 剩余 ≤ 1 天 → 紧急
REMINDER_DUE_SOON_DAYS: int = 3    # 剩余 ≤ 3 天 → 临期
REMINDER_NORMAL_DAYS: int = 7      # 剩余 ≤ 7 天 → 正常提醒

# ============================================================
# 六、辅助函数
# ============================================================


def ensure_directories() -> None:
    """创建运行所需的全部目录（幂等，可重复调用）。"""
    for directory in REQUIRED_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def is_llm_ready() -> bool:
    """判断大模型是否可用：只要配置了 API Key 即视为可用。

    注意：模块内部必须用 read_config() 读取，不能直接引用裸变量名
    （PEP 562 的模块级 __getattr__ 只对「模块外部」的属性访问生效）。
    """
    return bool(read_config("DEEPSEEK_API_KEY"))


def environment_warnings() -> list[str]:
    """启动自检：返回需要提醒用户的问题列表（为空表示一切正常）。

    只做「检查 + 提示」，不抛异常、不阻断启动：
    - 数据目录与数据库目录是否存在；
    - 知识库目录与关键知识文件是否就位；
    - 是否已配置大模型 API Key。

    部署到 Streamlit Cloud 时，若忘记配置 Secrets，本函数会给出明确提示，
    但应用仍可正常打开（浏览已有任务、导出报告等离线功能不受影响）。
    """
    warnings: list[str] = []

    # 1. 数据与数据库目录
    if not DATA_DIR.is_dir():
        warnings.append("数据目录缺失，任务数据将无法保存。")
    elif not DB_PATH.parent.is_dir():
        warnings.append("数据库目录缺失，任务数据将无法保存。")

    # 2. 知识库目录与关键文件
    missing_dirs = [
        directory.name
        for directory in (ORGANIZATION_DIR, RULES_DIR, TEMPLATES_DIR)
        if not directory.is_dir()
    ]
    if missing_dirs:
        warnings.append(f"知识库目录缺失（{'、'.join(missing_dirs)}），部门与规则知识不可用。")
    elif not (ORGANIZATION_DIR / "departments.json").is_file():
        warnings.append("知识库缺少部门职责文件（departments.json），部门名称将无法对齐。")

    # 3. 大模型配置
    if not is_llm_ready():
        warnings.append("大模型服务未配置，事项识别功能将在配置 API Key 后启用。")

    return warnings


def mask_secret(value: str, keep: int = 4) -> str:
    """对密钥做脱敏，仅保留首尾少量字符，用于界面展示与日志。"""
    if not value:
        return "（未配置）"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 6}{value[-keep:]}"


def config_summary() -> dict[str, object]:
    """返回配置概览，供首页「环境自检」区域展示。"""
    return {
        "应用名称": APP_NAME,
        "版本": APP_VERSION,
        "模型": read_config("DEEPSEEK_MODEL", _LAZY_CONFIG["DEEPSEEK_MODEL"]),
        "API 地址": read_config("DEEPSEEK_BASE_URL", _LAZY_CONFIG["DEEPSEEK_BASE_URL"]),
        "API Key": mask_secret(read_config("DEEPSEEK_API_KEY")),
        "大模型可用": is_llm_ready(),
        "数据库": str(DB_PATH),
        "调试模式": DEBUG,
    }


# 模块被导入时确保目录存在，避免首次运行报「路径不存在」
ensure_directories()
