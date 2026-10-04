# CampusFlow AI（高校基层事务闭环执行智能体）

基于大语言模型的**智能体工作流系统**，用于解决高校基层事务「事项无法形成闭环」的问题。

> 本项目不是聊天机器人，也不是普通的 AI 总结工具。
> 核心链路为：**识别 → 规划 → 执行 → 状态管理 → 汇总**。
> 会议纪要只是输入来源之一，系统同时支持 Word / PDF / TXT / Excel。

## 一、闭环流程

```
上传 Word / PDF / TXT / Excel
        ↓
Agent 解析文本
        ↓
识别事项（Task Extraction）
        ↓
提取负责人、部门、截止时间
        ↓
自动判断优先级
        ↓
人工确认
        ↓
写入数据库
        ↓
状态跟踪（待办 / 进行中 / 临期 / 完成 / 逾期）
        ↓
自动生成提醒
        ↓
生成阶段性工作报告
```

## 二、技术栈

| 类别 | 选型 |
| --- | --- |
| 语言 | Python 3.10 |
| 前端 | Streamlit 1.37.1（版本已钉死，避免样式漂移） |
| 模型 | OpenAI 兼容协议（硅基流动 SiliconFlow / DeepSeek 官方均可） |
| 数据库 | SQLite（单文件，无需服务端） |
| 部署 | Streamlit Cloud 或任意支持 Python 的公网环境（详见 [`docs/DEPLOY.md`](docs/DEPLOY.md)） |

**刻意不使用**：LangChain、MCP、微服务架构。项目采用轻量 Agent 架构（纯 Python + 函数调用 + JSON 协议）。

## 三、目录结构

> 本仓库根目录即项目根目录（等价于设计文档中的 `campusflow-ai/`）。

```
.
├── app.py                      # Streamlit 入口（导航 + 页面分发 + 启动自检）
├── requirements.txt            # 主依赖清单（保持简洁）
├── requirements-lock.txt       # 完整版本锁定（含间接依赖，复现环境用）
├── run.bat                     # Windows 一键启动（双击即可，固定使用 .venv）
├── config/
│   └── settings.py             # 统一配置中心（路径、模型、枚举、阈值、启动自检）
├── llm/                        # 大模型接入层
│   ├── client.py               # OpenAI 兼容客户端 + JSON 容错解析
│   └── prompts.py              # 所有 Prompt 集中存放
├── agent/                      # 核心 Agent（4 个）
│   ├── task_agent.py           # 事项识别（唯一调用大模型）
│   ├── planner_agent.py        # 优先级判定 / 风险分级 / 人工确认标记
│   ├── reminder_agent.py       # 临期、紧急、逾期提醒生成
│   └── report_agent.py         # 驾驶舱统计与阶段性报告
├── tools/                      # 无状态工具函数
│   ├── parser_tool.py          # Word/PDF/TXT/Excel 文本解析
│   ├── priority_tool.py        # 关键词规则打分
│   ├── report_tool.py          # 统计与 Markdown 渲染
│   └── seed_demo.py            # 演示数据种子脚本
├── database/                   # 数据层（Agent Memory）
│   ├── models.py               # 表结构定义（tasks / task_logs）
│   └── sqlite_db.py            # SQLite 连接、CRUD 与状态留痕
├── knowledge/                  # 轻量知识库（非 RAG，读文件后注入 Prompt）
│   ├── loader.py               # 统一加载器
│   ├── organization/           # 部门职责
│   ├── rules/                  # 优先级规则、提醒规则
│   └── templates/              # 会议纪要 / 任务 / 报告模板
├── ui/                         # 界面层
│   ├── components/             # 视觉规范、卡片、表格、状态、流水线编排
│   └── pages/                  # 五个页面
├── tests/                      # 测试（372 个用例）
├── docs/
│   ├── DEPLOY.md               # 部署说明（本地 / Streamlit Cloud / 安全）
│   └── dev/                    # 开发期临时脚本（不参与运行）
└── data/                       # 运行时数据（SQLite 文件、上传件），不入库
```

## 四、快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置密钥（复制模板后填入真实 Key，禁止硬编码）
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux

# 3. 运行
streamlit run app.py

# 4. 测试
pytest -q
```

> **Windows 用户**可直接双击项目根目录的 **`run.bat`** 一键启动。
> 它会固定使用项目虚拟环境 `.venv`，避免误用 Anaconda 等全局环境
> （Anaconda 自带的 pyarrow 与 NumPy 2.x 不兼容，会导致 Streamlit 无法启动）。
> 完整说明见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 五、开发原则

1. 每次只开发一个模块，完成一个模块先跑测试。
2. 不一次性生成整个项目。
3. 优先保证可运行，再优化界面。
4. 所有代码必须有中文注释。
5. 所有 Prompt 单独放入 `llm/prompts.py`。
6. 配置统一放 `config/settings.py`。
7. API Key 放 `.env`，不允许写死。

## 六、数据来源声明

- **真实数据**：学校公开规章制度、部门职责、管理办法。
- **模拟数据**：会议纪要、任务记录、状态变化，界面与报告中均标注为「演示数据（Synthetic Data）」。
- 禁止使用虚假政府公文。

## 七、当前进度

| 阶段 | 模块 | 状态 |
| --- | --- | --- |
| 1 | 项目目录与基础框架 | ✅ 已完成 |
| 2 | 解析工具（parser_tool）与 LLM 接入层（client / prompts） | ✅ 已完成 |
| 3 | TaskAgent 事项识别 | ✅ 已完成 |
| 4 | PlannerAgent 规划与优先级 | ✅ 已完成 |
| 5 | SQLite Agent Memory（tasks / task_logs） | ✅ 已完成 |
| 6 | ReminderAgent 主动督办 | ✅ 已完成 |
| 7 | ReportAgent 驾驶舱与报告 | ✅ 已完成 |
| 8 | 轻量知识库（部门职责 / 规则 / 模板） | ✅ 已完成 |
| 9 | 五页面产品化界面 | ✅ 已完成 |
| 10 | 部署工程化（依赖锁定 / Secrets / 启动自检） | ✅ 已完成 |

测试：`pytest -q` → **371 passed, 1 skipped**

## 八、部署

### 本地运行

```bash
pip install -r requirements.txt
copy .env.example .env      # 填入真实密钥
streamlit run app.py
```

### Streamlit Cloud

1. 推送到 GitHub（`.env`、`data/`、`.streamlit/secrets.toml` 已被忽略）；
2. 在 <https://share.streamlit.io> 新建应用并选择仓库；
3. Main file path 填 **`app.py`**；
4. 在 `Settings → Secrets` 填写密钥：

```toml
DEEPSEEK_API_KEY = "sk-你的真实密钥"
DEEPSEEK_BASE_URL = "https://api.siliconflow.cn/v1"
DEEPSEEK_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
```

配置读取优先级：**Streamlit Secrets > .env > 代码默认值**，
同一份代码在本地与云端都能直接运行，无需修改代码。

> 未配置密钥时应用仍可启动，侧边栏会给出提示，「事项识别」在配置后启用。
> 完整步骤与常见问题见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

### 依赖锁定

`requirements.txt` 为主依赖（简洁）；`requirements-lock.txt` 为完整版本快照，
需要完全复现环境时执行 `pip install -r requirements-lock.txt`。

## 九、密钥与安全

> ⚠️ **绝对不要把真实 API Key 上传到 GitHub。**

| 文件 | 是否入库 | 能否放真实密钥 |
| --- | --- | --- |
| `.env` | ❌ 已忽略 | ✅ 可以（本地使用） |
| `.streamlit/secrets.toml` | ❌ 已忽略 | ✅ 可以（本地调试 Secrets） |
| `.env.example` | ✅ 会入库 | ❌ **只能放占位符** |
| 源码 / README / 截图 / 演示视频 | ✅ 会入库 | ❌ 禁止出现真实密钥 |

提交前自查：

```bash
git status --short      # 确认 .env、data/、secrets.toml 未被跟踪
git grep -n "sk-"       # 搜索是否误提交了密钥
```

若密钥曾出现在任何会被提交的文件中，请视为已泄露，立即到服务商后台**作废并重建**。
