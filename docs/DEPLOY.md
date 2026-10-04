# CampusFlow AI 部署说明

本文档覆盖「本地运行」与「Streamlit Cloud 公网部署」两条路径，以及环境变量与安全要求。

---

## 1. 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Python | **3.10**(3.10.x) | 已在 3.10.11 实测 |
| 运行框架 | Streamlit **1.37.1** | 版本已钉死，见第 5 节说明 |
| 操作系统 | Windows / Linux / macOS | Streamlit Cloud 为 Linux |
| 数据库 | SQLite | Python 内置，无需单独安装 |
| 网络 | **需要联网** | 仅「事项识别」调用大模型 API；其余功能离线可用 |

---

## 2. 本地运行

```bash
# ① 安装依赖
pip install -r requirements.txt

# ② 配置密钥（复制模板后填入真实 Key）
copy .env.example .env          # Windows
cp .env.example .env            # macOS / Linux

# ③ 启动
streamlit run app.py

# ④ 运行测试（可选）
pytest -q
```

启动后浏览器访问 `http://localhost:8501`。

> 建议使用独立虚拟环境，避免与全局环境中的其他项目依赖冲突：
>
> ```bash
> python -m venv .venv
> .venv\Scripts\Activate.ps1      # Windows
> source .venv/bin/activate       # macOS / Linux
> pip install -r requirements.txt
> ```

### 2.1 Windows 一键启动（推荐）

Windows 用户可直接**双击项目根目录的 `run.bat`**，或在命令行执行：

```powershell
cd D:\liyou\workbuddy\demo\CampusFlow_AI
.\run.bat
```

`run.bat` 做了什么：

1. 自动切换到项目根目录，双击也能正确运行；
2. **固定使用项目虚拟环境 `.venv` 里的 Python**，不会误用 Anaconda 等全局环境；
3. `.venv` 不存在时给出创建命令并停止；`.env` 不存在时提醒但不阻断启动；
4. 启动后打印访问地址与停止方式，异常退出时保留窗口便于查看报错。

支持透传 Streamlit 参数，例如换端口启动：

```powershell
.\run.bat --server.port 8502
```

> 若 PowerShell 提示"禁止运行脚本"（执行策略限制），不必修改系统策略，
> 直接双击 `run.bat` 或使用 `.\.venv\Scripts\streamlit.exe run app.py` 即可。

---

## 3. Streamlit Cloud 部署

1. **推送到 GitHub**
   将项目推送到 GitHub 仓库（`.env`、`data/`、`.streamlit/secrets.toml` 已被 `.gitignore` 排除，不会上传）。

2. **连接 Streamlit Cloud**
   登录 <https://share.streamlit.io> → `New app` → 选择仓库与分支。

3. **选择入口文件**
   Main file path 填写：**`app.py`**（必须与仓库根目录一致）。

4. **配置 Secrets**（关键步骤）
   在 `Settings` → `Secrets` 中粘贴以下内容（替换为真实值）：

   ```toml
   DEEPSEEK_API_KEY = "sk-你的真实密钥"
   DEEPSEEK_BASE_URL = "https://api.siliconflow.cn/v1"
   DEEPSEEK_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
   ```

5. **部署并验证**
   点击 `Deploy`，等待构建完成；打开应用确认五个页面可访问，并在「会议材料上传」页试跑一次识别。

> 若第 4 步未配置，应用**仍可正常打开**（侧边栏会给出提示），但「事项识别」不可用；
> 浏览任务、更新状态、导出报告等离线功能不受影响。

---

## 4. 环境变量说明

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是（识别功能） | 空 | 大模型 API Key。为空时应用可启动，但事项识别不可用 |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | 接口地址（OpenAI 兼容协议）。硅基流动填 `https://api.siliconflow.cn/v1` |
| `DEEPSEEK_MODEL` | 否 | `deepseek-chat` | 模型 ID。硅基流动示例 `deepseek-ai/DeepSeek-V4-Flash` |
| `CAMPUSFLOW_DEBUG` | 否 | `0` | 置 `1` 时打印大模型原始返回，便于排错；正式演示请置 `0` |

### 读取优先级

```
Streamlit Secrets  >  .env  >  代码默认值
```

- **Streamlit Cloud**：读取 `Settings → Secrets`（即 `st.secrets`）；
- **本地**：读取项目根目录下的 `.env`；
- 两者都没有时使用代码默认值，并给出启动提示。

代码只读取**项目根目录下 `config/settings.py` 中声明的固定路径**（`BASE_DIR / ".env"`），
不会向上级目录搜寻、也不会读取项目外的配置文件。

> 实现说明：配置项采用「首次访问时才读取」的延迟加载方式。
> 原因是 Streamlit 要求 `set_page_config()` 必须是本轮运行的第一个命令，
> 若在模块导入阶段就去读 Secrets，会产生提示消息并导致启动报错。

---

## 5. 为什么 Streamlit 版本必须钉死

界面中「将文件拖拽到此处 / 选择文件」的中文化与侧边栏导航样式，
是通过 CSS 覆盖 Streamlit 组件结构（`data-testid`）实现的，**这些结构属于 1.37.1**。

因此：

- `requirements.txt` 中固定为 `streamlit==1.37.1`；
- 若升级到其他大版本，样式可能回退（**不会报错**，但中文标签会变回英文）；
- 如需升级，请同步回归：上传区中文化、侧边栏导航、五个页面渲染。

---

## 6. 安全要求（比赛提交前必读）

1. **禁止把真实 API Key 写进任何会被提交的文件**
   - `.env` ✅ 已被忽略，可放真实 Key；
   - `.env.example` ❌ 会被提交，**只允许放占位符**；
   - `.streamlit/secrets.toml` ✅ 已被忽略（本地调试 Secrets 时用）；
   - 源码 / README / 截图 / 演示视频中 ❌ 不得出现真实 Key。
2. **提交仓库前自查**
   ```bash
   git status --short          # 确认 .env、data/、secrets.toml 未被跟踪
   git grep -n "sk-"           # 搜索是否误提交了密钥
   ```
3. **一旦密钥曾出现在提交文件中**，视为已泄露，请立即到服务商后台**作废并重建**。
4. 演示 / 答辩前把 `CAMPUSFLOW_DEBUG` 置 `0`，避免日志刷屏。

---

## 7. 依赖版本锁定

| 文件 | 用途 |
| --- | --- |
| `requirements.txt` | 人工维护的主依赖清单，保持简洁 |
| `requirements-lock.txt` | 版本快照（含关键间接依赖），用于复现环境 |

需要完全复现时：

```bash
pip install -r requirements-lock.txt
```

若要生成「含全部间接依赖」的完整快照，在干净虚拟环境中执行：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip freeze > requirements-lock.txt
```

---

## 8. 部署自检清单

- [ ] `pip install -r requirements.txt` 无报错
- [ ] `pytest -q` 通过
- [ ] `streamlit run app.py` 后五个页面均可打开：智能驾驶舱 / 会议材料上传 / 事项智能识别 / 任务督办中心 / 执行情况汇报
- [ ] 侧边栏无启动告警（有告警说明数据目录、知识库或 API Key 有问题）
- [ ] 「会议材料上传」页上传一份文档能识别出事项
- [ ] 「执行情况汇报」页可生成并导出报告
- [ ] 仓库中不含 `.env`、`data/`、`.streamlit/secrets.toml`

---

## 9. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 走进度条后提示「调用大模型失败」 | API Key 未配置 / 余额不足 / 网络不通。本地查 `.env`，云端查 Secrets |
| 上传页提示「未检测到模型密钥」 | 同上一行；确认键名拼写为 `DEEPSEEK_API_KEY` |
| 侧边栏出现「数据目录缺失」提示 | 首次运行会自动创建；若失败请检查目录写入权限 |
| 上传区显示英文（Drag and drop） | Streamlit 版本不是 1.37.1，请按 `requirements.txt` 重装 |
| 任务列表为空 | 数据目录下的 `campusflow.db` 是新建的空库，先上传文档识别并确认入库 |
| 想清空演示数据 | 删除 `data/campusflow.db`，下次启动会自动重建空库 |
| 启动即报 `_ARRAY_API not found` 或 `numpy.core.multiarray failed to import` | **用错了 Python 环境**。常见于 Anaconda 自带的 pyarrow 与 NumPy 2.x 不兼容。请改用项目虚拟环境：双击 `run.bat`，或执行 `.\.venv\Scripts\streamlit.exe run app.py` |
| 报 `StreamlitAPIException: set_page_config() ...` | Streamlit 版本不是 1.37.1，按 `requirements.txt` 重装依赖 |
| 侧边栏出现 `No secrets files found` 红色提示 | 已在本项目修复（仅在 secrets 文件存在时才读取）；若仍出现说明代码版本过旧 |
