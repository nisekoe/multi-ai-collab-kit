# 环境接入指南（SETUP）

本指南把**「我们提供了什么」**、**「你需要在自己电脑上准备什么」**以及**「如何把四层治理甲在本地立起来」**一条一条讲清，方便你和自己的 AI 助手对照配置。

> **核心原则**：跨平台多 AI 协作不可能不经亲身体验与调整就一键完美适配所有电脑。本仓库提供完整的第 1–3 层判据、强制脚本、桥接插件与配置模板；第 4 层共用规则与 `入口.md` / `AGENTS.md` 指针需要你按照本指南立起来，并与你的 AI 助手讨论后按需裁剪（详见 [GOVERNANCE.md](GOVERNANCE.md)）。

---

## 快速开始

在仓库或解压后的工具目录执行以下命令。

```bash
python run_tests.py
python init_hub.py ./my-hub
python tools/init_rules.py --root ./my-hub
python tools/channel_params.py
```

`init_hub.py` 安装配置模板并绑定档案根。`init_rules.py` 写入该根的 `AGENTS.md`。通道检查只读配置。真实宿主接线与口令可稍后填写。

如需安装 Python 工具，先用 `python -m venv .venv` 创建并激活虚拟环境，再运行 `python -m pip install .`。已安装的 `mack-init-hub` 和 `mack-init-rules` 与源码中的初始化工具使用同一套模板。桥接脚本、发布与宿主插件的命令以仓库或附件目录为工作目录。

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
   - 从源码克隆、打包和仓库变更守护需要 Git。守护工具可以用 `KIT_GIT` 指定 Git 二进制。

---

## 二、档案根与规则

`init_hub.py` 在档案根生成 `.archive-root.json`、`入口.md`、`AGENTS.md`、`_state/通道.json`、`10-governance/守则/传球判据.json`、账本以及角色的 `scratch.md`。重复初始化保留已有文件。

- `python init_hub.py ./my-hub --id team-hub` 可以为新档案指定 ID。本机绑定保存该 ID，其他电脑可在绑定前用 `ARCHIVE_ID_EXPECTED` 指定预期值。
- 本机已有不同的档案指针时，可在当前进程设置 `ARCHIVE_ROOT`，或用 `python init_hub.py ./my-hub --force` 切换本机绑定。
- `python tools/paths.py --write` 用来核对当前写根。已有档案可以用 `python tools/paths.py --set-pointer ./my-hub` 手动绑定。
- `python tools/init_rules.py` 写入绑定档案根的 `AGENTS.md`。`--root ./my-hub` 可显式指定档案，`--target <文件>` 可写入其他客户端实际使用的规则文件。`--check` 核对规则。
- 若需要规则随正本变化自动更新，见 [规则安装与监视](docs/agents-injection.md)。

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
| `CARD_DIR` / `CARD_PORT` | 选窗卡片数据目录和端口 | Windows 优先 `%LOCALAPPDATA%/.multi-ai-collab-kit/cards`，其他情况用 `~/.multi-ai-collab-kit/cards`。端口 `8790` |
| `DELIVERY_HISTORY` | 四态投递历史文件 | 与卡片目录同级的 `投递历史.jsonl` |
| `KIT_CODEX_BIN` / `CODEX_HOME` | Codex 二进制及本机会话存储 | PATH 或 Windows 桌面端位置，存储默认为 `~/.codex` |
| `PORTA_BASE` | IDE 端本地 Porta 服务地址 | `http://127.0.0.1:3170` |
| `PROTECTED_WORKSPACES` | 受保护工作区关键词（逗号分隔，选窗器强制预排除） | `protected-workspace,private-zone` |
| `FEISHU_LEGACY_SESSION_PREFIX` | 飞书历史会话 ID 前缀技术约定（可覆盖） | `dfc:` |

---

## 五、挂载 Harbor 插件（DSH 侧）

1. 将 `plugins/dsh-plugin-harbor/` 目录放到 DSH 插件加载目录；
2. 参考 `templates/cordis.patch.yml` 在你的 profile 中挂载插件（注意 `name: dsh-plugin-harbor` 必填）；
3. 重启 DSH 后，访问 `/api/harbor/health`，然后运行 `python tools/channel_params.py --live` 验证目标会话。
