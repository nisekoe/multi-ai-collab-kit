# 环境接入指南（SETUP）

本指南把**「我们提供了什么」**、**「你需要在自己电脑上准备什么」**以及**「如何把四层治理甲在本地立起来」**一条一条讲清，方便你和自己的 AI 助手对照配置。

> **核心原则**：跨平台多 AI 协作不可能不经亲身体验与调整就一键完美适配所有电脑。本仓库提供完整的第 1–3 层判据、强制脚本、桥接插件与配置模板；第 4 层共用规则与 `入口.md` / `AGENTS.md` 指针需要你按照本指南立起来，并与你的 AI 助手讨论后按需裁剪（详见 [GOVERNANCE.md](GOVERNANCE.md)）。

---

## 一、你需要自备的东西

本仓库只提供本地桥接脚本、插件源码、配置模板与治理门禁，以下内容需在你本机自行准备：

1. **Python 3.10+**
   - 绝大多数模块仅依赖 Python 标准库，零外部依赖即可运行。
   - 若要使用读取 DSH 压缩会话日志的功能（`session_identity.py` / `comm_gate.py`），需额外安装 `zstandard`：
     ```bash
     pip install zstandard
     ```
2. **你实际使用的 AI 客户端与对应账号**
   - 桌面端（DSH）＋ 本仓库 `plugins/dsh-plugin-harbor` 插件；
   - IDE 端（如 Antigravity，启用本地 Porta 接口，默认 `PORTA_BASE=http://127.0.0.1:3170`）；
   - CLI 端（如 Codex CLI，用于 `reach_gpt.py` / `codex_picker_adapter.py`）。
   - *说明*：不需要三端全部装齐！哪怕只装了其中任意两端，也可以按需只启用对应的桥接腿与治理门禁。
3. **Git（可选）**
   - 仅在使用 `guard.py` / `sync_check.py` 做仓库变更守护时需要本机 `git` 在 `PATH` 中。

---

## 二、本机路径、六区骨架与入口指针配置（零硬编码）

所有工具统一通过 `paths.py` 的 4 级寻根链定位你的工作区，**代码里不写死任何盘符或个人路径**：

### 1. 创建六区目录骨架与必需指针文件
在任意盘符创建你的工作根目录（例如 `D:\my-ai-hub`），并建立以下目录与基础文件（**注意：`closeout.py` 收工对账会读取 `<ARCHIVE_ROOT>/入口.md`、账本与角色 `scratch.md`，请勿漏建**）：

```powershell
# PowerShell 示例：在 D:\my-ai-hub 建立完整骨架
$hub = "D:\my-ai-hub"
mkdir "$hub\_state", "$hub\00-config", "$hub\10-governance\守则", "$hub\10-governance\ledger" -Force
mkdir "$hub\20-projects\demo-alpha", "$hub\30-roles\planner_bot", "$hub\30-roles\review_bot", "$hub\30-roles\coder_bot", "$hub\90-archive" -Force

# 复制模板就位
copy templates\.archive-root.json "$hub\.archive-root.json"
copy templates\入口.md            "$hub\入口.md"
copy templates\AGENTS.md          "$hub\AGENTS.md"
copy templates\通道.json          "$hub\_state\通道.json"
copy templates\传球判据.json      "$hub\10-governance\守则\传球判据.json"
copy templates\窗口登记模板.json  "$hub\00-config\active_projects.json"

# 创建初始空白账本与角色临时本子
New-Item -ItemType File -Force "$hub\10-governance\ledger\工作台结果.md", "$hub\10-governance\ledger\待处理事项.md" | Out-Null
New-Item -ItemType File -Force "$hub\30-roles\planner_bot\scratch.md", "$hub\30-roles\review_bot\scratch.md", "$hub\30-roles\coder_bot\scratch.md" | Out-Null
```

### 2. 绑定根目录（二选一）
- **环境变量法（推荐）**：设置 `ARCHIVE_ROOT=D:\my-ai-hub`；
- **本机指针法**：运行一次：
  ```bash
  python paths.py --set-pointer D:\my-ai-hub
  ```
  它会在 `%LOCALAPPDATA%\agent-archive\root.txt` 写一行本机指针，以后每次运行自动识别，不污染仓库目录。

### 3. 注入第四层共用纪律（`AGENTS.md`）
- 打开刚复制到根目录的 `AGENTS.md`（并参考 [GOVERNANCE.md](GOVERNANCE.md)），将其配置为你各 AI 客户端的项目级系统提示词（如 `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`）。
- 只有让 AI 每轮读取这些纪律（如开工先跑 `comm_gate.py`、写文件走 `safe_write.py`、记账走 `ledger_lock.py`、收工跑 `closeout.py`），包内的第 1–3 层工具才会被 AI 真正串联调用。

---

## 三、地址、端口与凭据隔离（安全红线）

1. **统一通道正本**：编辑 `<ARCHIVE_ROOT>/_state/通道.json`（或通过 `DSH_CHANNEL_FILE` 环境变量指向自定义位置）。
2. **端口按需自定**：模板里的 `18097` / `18098` 为示例占位端口，请改成你本机实际监听的端口（切勿在未设强口令的情况下开启 `allowLan: true`）。
3. **凭据不入仓（`secrets_dir`）**：
   - `通道.json` 中的 `token` 字段可以留空，改为填写 `token_secret`（如 `harbor_desktop`）；
   - 把真实口令单独放进库外目录 `%LOCALAPPDATA%\agent-archive\secrets\harbor_desktop.txt`（可通过 `ARCHIVE_SECRETS_DIR` 覆盖）；
   - 这样就算把整个工作区打包或提交 Git，也绝不会泄露真实口令。

---

## 四、常用环境变量一览

| 环境变量 | 作用 | 默认值 |
| :--- | :--- | :--- |
| `ARCHIVE_ROOT` | 档案库 / 工作台根目录路径 | 无（可由本机指针提供） |
| `ARCHIVE_ID_EXPECTED` | 哨兵校验期望的 `archive_id` | `user-multiagent-hub` |
| `ARCHIVE_SECRETS_DIR` | 库外凭据目录（放 `.txt` 口令文件） | `%LOCALAPPDATA%\agent-archive\secrets` |
| `DSH_CHANNEL_FILE` | 覆盖 `通道.json` 路径 | `<ARCHIVE_ROOT>/_state/通道.json` |
| `PORTA_BASE` | IDE 端本地 Porta 服务地址 | `http://127.0.0.1:3170` |
| `PROTECTED_WORKSPACES` | 受保护工作区关键词（逗号分隔，选窗器强制预排除） | `protected-workspace,private-zone` |
| `FEISHU_LEGACY_SESSION_PREFIX` | 飞书历史会话 ID 前缀技术约定（可覆盖） | `dfc:` |

---

## 五、挂载 Harbor 插件（DSH 侧）

1. 将 `plugins/dsh-plugin-harbor/` 目录放到 DSH 插件加载目录；
2. 参考 `templates/cordis.patch.yml` 在你的 profile 中挂载插件（注意 `name: dsh-plugin-harbor` 必填）；
3. 重启 DSH 后，访问 `/api/harbor/health` 或运行 `python channel_params.py` 验证连通性。
