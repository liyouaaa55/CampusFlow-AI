"""运行时就绪自检（preflight）：回答「当前是否具备比赛演示条件」。

与既有 environment_warnings() 的区别：后者只做「配置字符串非空 + 目录存在」
的静态判断；本模块做真实探测——真的建临时文件并解析、真的开 SQLite 写库再
读回、真的调用各 Agent 公开接口、真的跑完整业务链路并断言数据库发生变化。

约束：
- 只读不污：写操作都在临时目录 / 临时库进行，绝不触碰 data/campusflow.db。
- 不抛异常：任何探测失败都转成带修复建议的检查项。
- 分层执行：deep=False 不发起网络请求；deep=True 真实调用大模型。
"""

from __future__ import annotations

import platform
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from agent import execution_agent, planner_agent, reminder_agent, report_agent, task_agent
from config import settings
from database import models, sqlite_db
from tools import execution_tool, parser_tool

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

CATEGORIES = ("环境", "基础设施", "解析能力", "大模型", "智能体", "业务链路")

# 关键链路环节（与产品主张一一对应）
CHAIN_STEPS = (
    "上传落盘",
    "文件解析",
    "AI 事项识别",
    "人工确认入库",
    "督办读取",
    "任务智能分析",
    "执行动作",
    "执行留痕",
    "驾驶舱与汇报",
)

@dataclass
class CheckItem:
    """单条检查结果。"""

    category: str
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def blocking(self) -> bool:
        """是否属于阻断项（决定整体结论）。"""
        return self.status == FAIL


@dataclass
class CheckReport:
    """一次自检的完整结果。"""

    items: list[CheckItem] = field(default_factory=list)
    today: date = field(default_factory=date.today)
    deep: bool = False

    def add(self, item: CheckItem) -> None:
        self.items.append(item)

    def extend(self, items: list[CheckItem]) -> None:
        self.items.extend(items)

    def by_category(self, category: str) -> list[CheckItem]:
        return [item for item in self.items if item.category == category]

    @property
    def counts(self) -> dict[str, int]:
        result = {PASS: 0, WARN: 0, FAIL: 0}
        for item in self.items:
            result[item.status] = result.get(item.status, 0) + 1
        return result

    @property
    def ok(self) -> bool:
        """是否可以进入演示：无 FAIL 项。"""
        return all(item.status != FAIL for item in self.items)

    def first_failure(self) -> CheckItem | None:
        for item in self.items:
            if item.status == FAIL:
                return item
        return None


# ============================================================
# 一、环境
# ============================================================


def check_environment() -> list[CheckItem]:
    """Python 版本、关键依赖、.env 与 API 配置。"""
    items: list[CheckItem] = []

    version = platform.python_version()
    major_minor = ".".join(version.split(".")[:2])
    ok = major_minor >= "3.10"
    items.append(
        CheckItem(
            "环境",
            "Python 版本",
            PASS if ok else FAIL,
            f"当前 {version}",
            "" if ok else "项目要求 Python 3.10 及以上",
        )
    )

    # 关键依赖逐个探测：缺任何一个都会让对应功能在演示时崩溃
    required = (
        ("streamlit", "界面框架"),
        ("openai", "大模型客户端"),
        ("docx", "Word 解析"),
        ("pdfplumber", "PDF 解析"),
        ("pandas", "表格解析"),
        ("openpyxl", "Excel 引擎"),
    )
    missing: list[str] = []
    for module_name, purpose in required:
        try:
            __import__(module_name)
        except Exception:  # noqa: BLE001
            missing.append(f"{module_name}（{purpose}）")
    items.append(
        CheckItem(
            "环境",
            "关键依赖",
            PASS if not missing else FAIL,
            "全部就绪" if not missing else "缺少：" + "、".join(missing),
            "执行 pip install -r requirements.txt" if missing else "",
        )
    )

    env_exists = settings.ENV_FILE.is_file()
    items.append(
        CheckItem(
            "环境",
            ".env 文件",
            PASS if env_exists else WARN,
            str(settings.ENV_FILE) if env_exists else "未找到 .env",
            "复制 .env.example 为 .env 并填入密钥" if not env_exists else "",
        )
    )

    key = settings.read_config("DEEPSEEK_API_KEY")
    base_url = settings.read_config("DEEPSEEK_BASE_URL")
    model = settings.read_config("DEEPSEEK_MODEL")
    has_key = bool(key)
    items.append(
        CheckItem(
            "环境",
            "API 配置完整性",
            PASS if has_key else FAIL,
            f"模型={model}；地址={base_url}；密钥={settings.mask_secret(key)}",
            "在 .env 中填写 DEEPSEEK_API_KEY" if not has_key else "",
        )
    )
    if has_key and not base_url.startswith("http"):
        items.append(
            CheckItem(
                "环境",
                "BASE_URL 格式",
                WARN,
                base_url,
                "地址应以 http(s) 开头，否则真实调用必然失败",
            )
        )

    return items


# ============================================================
# 二、基础设施
# ============================================================


def check_infrastructure() -> list[CheckItem]:
    """数据目录、上传目录、SQLite 真实读写。"""
    items: list[CheckItem] = []

    for name, directory in (("数据目录", settings.DATA_DIR), ("上传目录", settings.UPLOAD_DIR)):
        exists = directory.is_dir()
        items.append(
            CheckItem(
                "基础设施",
                name,
                PASS if exists else FAIL,
                str(directory) if exists else f"{directory} 不存在",
                "" if exists else "检查目录权限，或删除该目录让应用自动重建",
            )
        )

    # SQLite：真实建临时库、真实建表、真实写入再读回
    try:
        with tempfile.TemporaryDirectory(prefix="cf_preflight_") as tmp:
            probe = Path(tmp) / "probe.db"
            sqlite_db.init_db(probe)
            created = sqlite_db.batch_create(
                [
                    {
                        "title": "自检探针任务",
                        "department": "自检部门",
                        "owner": "自检人",
                        "deadline": (date.today() + timedelta(days=3)).isoformat(),
                        "priority": "medium",
                        "description": "由 preflight 创建，随后应能被读回",
                    }
                ],
                db_path=probe,
            )
            rows = sqlite_db.list_tasks(db_path=probe)
            logs = sqlite_db.get_logs(created[0]["id"], db_path=probe)
        ok = len(created) == 1 and len(rows) == 1 and len(logs) >= 1
        items.append(
            CheckItem(
                "基础设施",
                "SQLite 临时库写入读回",
                PASS if ok else FAIL,
                f"写入 {len(created)} 条 / 读回 {len(rows)} 条 / 日志 {len(logs)} 条",
                "" if ok else "临时库读写异常，磁盘可能不可写或 SQLite 被占用",
            )
        )
    except Exception as exc:  # noqa: BLE001
        items.append(
            CheckItem("基础设施", "SQLite 临时库写入读回", FAIL, f"{type(exc).__name__}: {exc}")
        )

    # 正式库只读可读性（不写入）
    try:
        total = len(sqlite_db.list_tasks())
        items.append(
            CheckItem(
                "基础设施",
                "正式任务库可读",
                PASS,
                f"{settings.DB_PATH.name} 中现有 {total} 条任务",
            )
        )
    except Exception as exc:  # noqa: BLE001
        items.append(
            CheckItem(
                "基础设施",
                "正式任务库可读",
                FAIL,
                f"{type(exc).__name__}: {exc}",
                "数据库可能已损坏，可删除 data/campusflow.db 让应用重建",
            )
        )

    return items


# ============================================================
# 三、解析能力（真实建临时文件 → 真实解析）
# ============================================================


def check_parsers() -> list[CheckItem]:
    """五种格式各造一个真实临时文件并解析，验证解析器真的能工作。"""
    items: list[CheckItem] = []
    sample = "自检样例：保卫处 王老师 负责完成宿舍消防隐患专项排查，2026-09-30 前建立整改台账。"

    with tempfile.TemporaryDirectory(prefix="cf_parse_") as tmp:
        root = Path(tmp)

        # Word
        try:
            from docx import Document

            doc = Document()
            doc.add_paragraph(sample)
            target = root / "probe.docx"
            doc.save(str(target))
            text = parser_tool.parse_file(target)
            ok = "消防隐患" in text
            items.append(
                CheckItem(
                    "解析能力",
                    "Word（.docx）",
                    PASS if ok else FAIL,
                    f"解析 {len(text)} 字" if ok else "未解析出关键内容",
                    "" if ok else "python-docx 安装异常或文档结构不兼容",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(CheckItem("解析能力", "Word（.docx）", FAIL, f"{type(exc).__name__}: {exc}"))

        # Excel
        try:
            import pandas as pd

            frame = pd.DataFrame({"事项": ["自检样例"], "截止时间": ["2026-09-30"]})
            target = root / "probe.xlsx"
            frame.to_excel(target, index=False)
            text = parser_tool.parse_file(target)
            ok = "自检样例" in text
            items.append(
                CheckItem(
                    "解析能力",
                    "Excel（.xlsx）",
                    PASS if ok else FAIL,
                    f"解析 {len(text)} 字" if ok else "未解析出关键内容",
                    "" if ok else "openpyxl 引擎异常",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(CheckItem("解析能力", "Excel（.xlsx）", FAIL, f"{type(exc).__name__}: {exc}"))

        # CSV / TXT / Markdown
        for label, filename, payload in (
            ("CSV（.csv）", "probe.csv", "事项,截止时间\n自检样例,2026-09-30\n"),
            ("纯文本（.txt）", "probe.txt", sample + "\n"),
            ("Markdown（.md）", "probe.md", "# 自检\n\n" + sample + "\n"),
        ):
            try:
                target = root / filename
                target.write_text(payload, encoding="utf-8")
                text = parser_tool.parse_file(target)
                ok = "自检样例" in text or "消防隐患" in text
                items.append(
                    CheckItem(
                        "解析能力",
                        label,
                        PASS if ok else FAIL,
                        f"解析 {len(text)} 字" if ok else "未解析出关键内容",
                        "" if ok else "文本解析异常",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                items.append(CheckItem("解析能力", label, FAIL, f"{type(exc).__name__}: {exc}"))

        # PDF：无法自动生成真实 PDF（项目未引入 PDF 生成库），
        # 因此只验证 pdfplumber 可导入，并明确标注需人工验证真实 PDF 抽取。
        try:
            import pdfplumber  # noqa: F401

            items.append(
                CheckItem(
                    "解析能力",
                    "PDF（.pdf）",
                    WARN,
                    "pdfplumber 可导入；未做真实 PDF 内容抽取验证",
                    "自动化无法覆盖：请在界面手动上传一份真实 PDF 确认抽取正常",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                CheckItem("解析能力", "PDF（.pdf）", FAIL, f"pdfplumber 不可用：{exc}")
            )

    return items


# ============================================================
# 四、大模型
# ============================================================


def check_llm(deep: bool = False) -> list[CheckItem]:
    """快速模式只读配置；deep 模式真实调用一次最小请求验证连通性与密钥。"""
    from llm import client

    items: list[CheckItem] = []
    ready = settings.is_llm_ready()
    items.append(
        CheckItem(
            "大模型",
            "密钥配置",
            PASS if ready else FAIL,
            f"模型={settings.read_config('DEEPSEEK_MODEL')}；地址={settings.read_config('DEEPSEEK_BASE_URL')}",
            "在 .env 中填写 DEEPSEEK_API_KEY" if not ready else "",
        )
    )

    if not ready:
        items.append(
            CheckItem(
                "大模型",
                "API 连通性",
                FAIL,
                "未配置密钥，无法验证",
                "配置密钥后重新运行 deep 自检",
            )
        )
        return items

    if not deep:
        items.append(
            CheckItem(
                "大模型",
                "API 连通性",
                WARN,
                "快速模式未发起网络请求",
                "赛前请运行 deep 自检（或手动识别一条事项）确认密钥有效",
            )
        )
        return items

    # deep 模式：真实调用。用最小 prompt，避免浪费额度。
    try:
        payload = client.chat_json(
            "你是连通性测试助手，只输出 JSON。",
            '请只输出这个 JSON 对象：{"ok": true}',
        )
        ok = isinstance(payload, (dict, list))
        items.append(
            CheckItem(
                "大模型",
                "API 连通性",
                PASS if ok else WARN,
                f"真实调用成功，返回类型 {type(payload).__name__}",
                "" if ok else "模型返回不是 JSON，事项识别可能失败",
            )
        )
    except Exception as exc:  # noqa: BLE001
        items.append(
            CheckItem(
                "大模型",
                "API 连通性",
                FAIL,
                f"{type(exc).__name__}: {exc}",
                "检查密钥是否有效、BASE_URL 是否正确、账户余额是否充足",
            )
        )

    return items


# ============================================================
# 五、智能体与知识库
# ============================================================

# 智能体名 → (模块, 必须存在的入口函数)
AGENT_ENTRYPOINTS: tuple[tuple[str, Any, str], ...] = (
    ("TaskAgent（事项识别）", task_agent, "extract_tasks"),
    ("PlannerAgent（规划）", planner_agent, "plan"),
    ("ReminderAgent（督办）", reminder_agent, "check_all"),
    ("ReportAgent（报告）", report_agent, "generate_weekly_report"),
    ("ExecutionAgent（处置分析）", execution_agent, "analyze"),
)

# 知识库关键文件（缺失会导致规则兜底失效或演示材料加载失败）
KNOWLEDGE_FILES: tuple[tuple[str, Path], ...] = (
    ("部门职责", settings.ORGANIZATION_DIR / "departments.json"),
    ("优先级规则", settings.RULES_DIR / "priority.json"),
    ("提醒规则", settings.RULES_DIR / "reminder.json"),
    ("报告模板", settings.TEMPLATES_DIR / "report.json"),
)


def check_agents() -> list[CheckItem]:
    """五个 Agent 的公开入口是否可用，以及规则文件是否齐备。"""
    items: list[CheckItem] = []

    for label, module, entry in AGENT_ENTRYPOINTS:
        func = getattr(module, entry, None)
        ok = callable(func)
        items.append(
            CheckItem(
                "智能体",
                label,
                PASS if ok else FAIL,
                f"入口 {entry}() 可调用" if ok else f"缺少入口 {entry}()",
                "" if ok else "模块导入失败或入口被误删",
            )
        )

    missing = [f"{label}（{path.name}）" for label, path in KNOWLEDGE_FILES if not path.is_file()]
    items.append(
        CheckItem(
            "智能体",
            "规则与模板文件",
            PASS if not missing else FAIL,
            "全部就绪" if not missing else "缺少：" + "、".join(missing),
            "" if not missing else "从仓库恢复 knowledge/ 目录",
        )
    )

    # 演示用内置材料：「一键体验演示」依赖它，缺失时演示按钮会报错
    try:
        from ui.components import pipeline

        demo_text = pipeline.load_demo_document()
        ok = bool(demo_text.strip())
        items.append(
            CheckItem(
                "智能体",
                "内置演示材料",
                PASS if ok else FAIL,
                f"已加载 {len(demo_text)} 字" if ok else "未找到内置演示文档",
                "" if ok else "检查 knowledge/templates/ 下的示例材料是否完整",
            )
        )
    except Exception as exc:  # noqa: BLE001
        items.append(CheckItem("智能体", "内置演示材料", FAIL, f"{type(exc).__name__}: {exc}"))

    return items


# ============================================================
# 六、业务链路（真实跑通九段，并断言数据库确实发生变化）
# ============================================================

# 链路用的固定材料：内容确定，便于复现
CHAIN_SAMPLE = (
    "自检链路样例会议纪要：\n"
    "一、保卫处 王老师 负责完成宿舍消防隐患专项排查，2026-09-30 前建立整改台账并完成销号。\n"
    "二、教务处 李主任 负责完成下学期教材征订统计，2026-09-25 前提交汇总表。\n"
)

# 打桩 LLM 的固定返回：TaskAgent 需要任务数组，ExecutionAgent 需要分析对象
STUB_TASK_PAYLOAD = [
    {
        "title": "完成宿舍消防隐患专项排查",
        "department": "保卫处",
        "owner": "王老师",
        "deadline": "2026-09-30",
        "priority": "high",
        "status": "pending",
        "description": "建立整改台账并完成隐患销号",
    }
]
STUB_ANALYSIS_PAYLOAD = {
    "situation": "该事项已超过截止日期且仍处于待办状态，需要立即处置。",
    "reasons": ["当前日期已超过截止日期。", "任务状态仍为待办。"],
}


def _stub_llm(payload: Any) -> Callable[[str, str], Any]:
    """构造一个固定返回的假模型（仅供快速模式自检使用）。"""

    def _call(system_prompt: str, user_prompt: str) -> Any:
        return payload

    return _call


def _dispatch_stub(system_prompt: str, user_prompt: str) -> Any:
    """按 Prompt 内容区分 TaskAgent 与 ExecutionAgent 的返回体。

    签名与 llm.client.chat_json 一致（system, user），否则 Agent 调用会 TypeError。
    """
    if "处置" in user_prompt or "判断依据" in user_prompt or "situation" in user_prompt:
        return STUB_ANALYSIS_PAYLOAD
    return STUB_TASK_PAYLOAD


def check_pipeline(
    deep: bool = False,
    llm_fn: Callable[[str, str], Any] | None = None,
    workdir: Path | None = None,
) -> list[CheckItem]:
    """真实跑通：落盘 → 解析 → 识别 → 入库 → 督办 → 分析 → 执行 → 留痕 → 报告。

    参数：
        deep    True 时用真实模型；False 时用打桩模型。
        llm_fn  注入自定义模型函数（测试用）。
        workdir 指定工作目录；为 None 时使用临时目录并在结束后删除，
                非 None 时**保留**目录内容，便于测试直接检查真实库与上传文件。
    """
    items: list[CheckItem] = []
    caller: Callable[[str, str], Any] | None = llm_fn

    if caller is None and not deep:
        caller = _dispatch_stub

    # 工作目录：指定 workdir 时保留内容供测试检查，否则用临时目录并在结束时清理
    temp_ctx = None
    if workdir is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="cf_chain_")
        root = Path(temp_ctx.name)
    else:
        root = Path(workdir)
        root.mkdir(parents=True, exist_ok=True)

    try:
        upload_dir = root / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        db_path = root / "chain.db"

        # ---------- 1. 上传落盘（复刻 upload_page._save_upload 的落盘行为） ----------
        try:
            source = upload_dir / "自检链路样例.txt"
            source.write_text(CHAIN_SAMPLE, encoding="utf-8")
            ok = source.is_file()
            items.append(
                CheckItem(
                    "业务链路",
                    "1. 上传落盘",
                    PASS if ok else FAIL,
                    f"已落盘 {source.name}（{source.stat().st_size} 字节）" if ok else "落盘失败",
                    "" if ok else "检查上传目录写权限",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(CheckItem("业务链路", "1. 上传落盘", FAIL, f"{type(exc).__name__}: {exc}"))
            return items

        # ---------- 2. 文件解析 ----------
        try:
            text = parser_tool.parse_file(source)
            ok = "消防隐患" in text
            items.append(
                CheckItem(
                    "业务链路",
                    "2. 文件解析",
                    PASS if ok else FAIL,
                    f"解析出 {len(text)} 字" if ok else "未解析出关键内容",
                    "" if ok else "解析器异常",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(CheckItem("业务链路", "2. 文件解析", FAIL, f"{type(exc).__name__}: {exc}"))
            return items

        # ---------- 3. AI 事项识别 ----------
        try:
            recognized = task_agent.extract_tasks(text, source_type="text", llm_fn=caller)
            ok = len(recognized) >= 1
            items.append(
                CheckItem(
                    "业务链路",
                    "3. AI 事项识别",
                    PASS if ok else FAIL,
                    f"识别出 {len(recognized)} 条事项" + ("（真实模型）" if deep or llm_fn else "（打桩模型）"),
                    "" if ok else "识别结果为空：检查 Prompt、模型返回或材料本身是否含事项",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                CheckItem("业务链路", "3. AI 事项识别", FAIL, f"{type(exc).__name__}: {exc}")
            )
            return items

        # ---------- 4. 人工确认入库（复刻 review_page._import_tasks） ----------
        try:
            sqlite_db.init_db(db_path)
            planned = planner_agent.plan(recognized)
            created = sqlite_db.batch_create(planned, db_path=db_path)
            rows = sqlite_db.list_tasks(db_path=db_path)
            ok = len(created) >= 1 and len(rows) == len(created)
            items.append(
                CheckItem(
                    "业务链路",
                    "4. 人工确认入库",
                    PASS if ok else FAIL,
                    f"入库 {len(created)} 条，列表读回 {len(rows)} 条" if ok else f"入库 {len(created)} / 读回 {len(rows)}",
                    "" if ok else "写入或读取不一致",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                CheckItem("业务链路", "4. 人工确认入库", FAIL, f"{type(exc).__name__}: {exc}")
            )
            return items

        # ---------- 5. 督办读取 ----------
        try:
            listed = sqlite_db.list_tasks(db_path=db_path)
            reminders = reminder_agent.check_all(db_path=db_path)
            ok = len(listed) >= 1
            items.append(
                CheckItem(
                    "业务链路",
                    "5. 督办读取",
                    PASS if ok else FAIL,
                    f"督办中心可读 {len(listed)} 条，生成提醒 {len(reminders)} 条",
                    "" if ok else "督办页读取不到任务",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                CheckItem("业务链路", "5. 督办读取", FAIL, f"{type(exc).__name__}: {exc}")
            )

        # ---------- 6. 任务智能分析 ----------
        # 关键：必须造一个「已逾期」任务，否则 analyze() 会按设计直接返回 None
        # （任务已完成 / 缺少截止时间 / 等级正常三种情况不调用模型），
        # 那样就测不到「智能分析」这条链路是否真的可用。
        overdue_id: int | None = None
        plan: dict[str, Any] | None = None
        try:
            overdue = sqlite_db.create_task(
                {
                    "title": "自检用逾期任务（用于触发智能分析）",
                    "department": "保卫处",
                    "owner": "自检人",
                    "deadline": (date.today() - timedelta(days=2)).isoformat(),
                    "priority": "high",
                    "description": "已超过截止日期，应触发智能处置分析",
                },
                db_path=db_path,
            )
            overdue_id = int(overdue["id"])
            plan = execution_agent.analyze(
                overdue_id, llm_fn=caller, db_path=db_path, today=date.today()
            )
            ok = isinstance(plan, dict) and bool(plan.get("action"))
            detail = (
                f"分析结论：{plan.get('action')}（等级 {plan.get('level')}）"
                if isinstance(plan, dict)
                else "analyze() 返回 None：该任务不满足处置条件，此链路未被验证"
            )
            items.append(
                CheckItem(
                    "业务链路",
                    "6. 任务智能分析",
                    PASS if ok else FAIL,
                    detail,
                    "" if ok else "逾期任务未产生处置建议：检查 ExecutionAgent 与模型返回",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                CheckItem("业务链路", "6. 任务智能分析", FAIL, f"{type(exc).__name__}: {exc}")
            )

        # ---------- 7. 执行动作 + 8. 执行留痕 ----------
        # 严格复刻 ui/pages/tasks_page.py 的 confirm_execution 调用序列
        if overdue_id is not None and isinstance(plan, dict) and plan.get("action"):
            try:
                before_logs = sqlite_db.get_logs(overdue_id, db_path=db_path)
                task = sqlite_db.get_task(overdue_id, db_path=db_path)
                logs = sqlite_db.get_logs(overdue_id, db_path=db_path)
                validation = execution_tool.validate_plan(
                    task, dict(plan), logs, today=date.today()
                )
                if not validation.ok:
                    items.append(
                        CheckItem(
                            "业务链路",
                            "7. 执行动作",
                            FAIL,
                            f"校验未通过：{validation.reason}",
                            "动作选择与校验规则冲突，需排查 execution_tool",
                        )
                    )
                else:
                    action = plan["action"]
                    remark = execution_tool.build_remark(
                        action, plan.get("level"), plan.get("days_left"), plan.get("situation", "")
                    )
                    # 人工确认动作留痕
                    sqlite_db.append_log(
                        overdue_id,
                        action=models.ACTION_CONFIRM,
                        operator="preflight",
                        remark=remark,
                        db_path=db_path,
                    )
                    # 受控执行
                    outcome = execution_tool.execute(
                        action,
                        task=task,
                        params=plan.get("params") or {},
                        operator="preflight",
                        remark=remark,
                        today=date.today(),
                        db_path=db_path,
                    )
                    # 补写动作事件（escalate 的事件由 update_risk_level 内部写入）
                    event = execution_tool.ACTION_LOG_EVENT.get(action)
                    if event and event != models.ACTION_ESCALATE:
                        sqlite_db.append_log(
                            overdue_id,
                            action=event,
                            operator="preflight",
                            remark=execution_tool.build_remark(
                                action,
                                plan.get("level"),
                                plan.get("days_left"),
                                str(outcome.get("message", "")),
                            ),
                            db_path=db_path,
                        )

                    after_logs = sqlite_db.get_logs(overdue_id, db_path=db_path)
                    grew = len(after_logs) > len(before_logs)
                    actions = {row.get("action") for row in after_logs}
                    expect_confirm = models.ACTION_CONFIRM in actions
                    items.append(
                        CheckItem(
                            "业务链路",
                            "7. 执行动作",
                            PASS,
                            f"动作 {action} 执行成功：{outcome.get('message', '')}",
                        )
                    )
                    items.append(
                        CheckItem(
                            "业务链路",
                            "8. 执行留痕",
                            PASS if grew and expect_confirm else FAIL,
                            f"日志 {len(before_logs)} → {len(after_logs)} 条，"
                            f"含人工确认={expect_confirm}，动作集合={sorted(a for a in actions if a)}",
                            "" if grew and expect_confirm else "执行后日志未增加或缺少确认记录",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                items.append(
                    CheckItem("业务链路", "7. 执行动作", FAIL, f"{type(exc).__name__}: {exc}")
                )
        else:
            items.append(
                CheckItem(
                    "业务链路",
                    "7. 执行动作",
                    FAIL,
                    "上游未产出可执行动作，跳过（执行链路未被验证）",
                    "先修复「6. 任务智能分析」",
                )
            )
            items.append(
                CheckItem("业务链路", "8. 执行留痕", FAIL, "上游未执行，跳过（留痕链路未被验证）")
            )

        # ---------- 9. 驾驶舱与汇报 ----------
        try:
            dashboard = report_agent.generate_dashboard(today=date.today(), db_path=db_path)
            weekly = report_agent.generate_weekly_report(today=date.today(), db_path=db_path)
            total = int(dashboard.get("total") or 0)
            ok = total >= 1 and bool(weekly.strip())
            items.append(
                CheckItem(
                    "业务链路",
                    "9. 驾驶舱与汇报",
                    PASS if ok else FAIL,
                    f"驾驶舱读到 {total} 条任务，周报生成 {len(weekly)} 字",
                    "" if ok else "报表未读到刚写入的数据",
                )
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                CheckItem("业务链路", "9. 驾驶舱与汇报", FAIL, f"{type(exc).__name__}: {exc}")
            )

    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()

    return items


# ============================================================
# 七、总入口
# ============================================================


def run_all(deep: bool = False, llm_fn: Callable[[str, str], Any] | None = None) -> CheckReport:
    """执行全部检查，返回结构化结果。

    参数：
        deep    True 时真实调用大模型（识别 + 分析 + 连通性探活）；
                False 时只用配置检查与打桩模型，毫秒级完成。
        llm_fn  注入自定义模型函数（测试用）；为 None 且 deep=True 时用真实模型。
    """
    report = CheckReport(deep=deep)
    report.extend(check_environment())
    report.extend(check_infrastructure())
    report.extend(check_parsers())
    report.extend(check_llm(deep=deep))
    report.extend(check_agents())
    report.extend(check_pipeline(deep=deep, llm_fn=llm_fn))
    return report


_STATUS_MARK = {PASS: "[通过]", WARN: "[提醒]", FAIL: "[失败]"}


def render_text(report: CheckReport) -> str:
    """渲染为中文纯文本报告（供命令行与日志使用）。"""
    counts = report.counts
    lines: list[str] = [
        "CampusFlow AI 运行时就绪自检",
        f"模式：{'深度（真实调用大模型）' if report.deep else '快速（不发起网络请求）'}",
        f"日期：{report.today.isoformat()}",
        "",
    ]
    for category in CATEGORIES:
        items = report.by_category(category)
        if not items:
            continue
        lines.append(f"── {category} " + "─" * max(0, 46 - len(category)))
        for item in items:
            line = f"  {_STATUS_MARK.get(item.status, '[?]')} {item.name}"
            if item.detail:
                line += f"：{item.detail}"
            lines.append(line)
            if item.hint and item.status != PASS:
                lines.append(f"        → 处理建议：{item.hint}")
        lines.append("")

    lines.append("─" * 52)
    lines.append(
        f"合计 {len(report.items)} 项：通过 {counts[PASS]}，提醒 {counts[WARN]}，失败 {counts[FAIL]}"
    )
    if report.ok:
        lines.append("结论：具备演示条件（无阻断项）。")
    else:
        first = report.first_failure()
        suffix = f"，首个阻断项：{first.category} / {first.name}" if first else ""
        lines.append(f"结论：暂不具备演示条件（存在阻断项）{suffix}")
    return "\n".join(lines)
