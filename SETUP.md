# 环境接入指南（SETUP）

本指南说明**本机需要自备什么**、**如何初始化最小工作根**以及**如何接入实际使用的 AI 客户端**。

---

## 快速开始

在仓库或解压后的工具目录执行以下命令：

```bash
python run_tests.py
python init_hub.py ./my-hub
python tools/init_rules.py --root ./my-hub
python tools/channel_params.py
```

`init_hub.py` 创建最小运行目录并写入本机指针；`init_rules.py` 将通用规则写入该根的 `AGENTS.md`；`channel_params.py` 离线校验通道配置（无需真实宿主与口令即可跑通）。

如需安装命令行入口，先创建并激活虚拟环境，再运行 `python -m pip install .`。安装后可在任意目录使用 `mack-init-hub` 和 `mack-init-rules`。

---

## 一、你需要自备的东西

1. **Python 3.10+**
   - 核心模块仅依赖 Python 标准库，零外部依赖即可运行。
   - 若要读取 DSH 压缩会话日志（`session_identity.py`），可按需安装 `zstandard`：`pip install zstandard`。
2. **你实际使用的 AI 客户端与对应账号**
   - 桌面端（DSH）＋ 本仓库 `plugins/dsh-plugin-harbor` 插件；
   - IDE 端（Antigravity，启用本地 Porta 接口，默认 `PORTA_BASE=http://127.0.0.1:3170`）；
   - CLI 端（Codex CLI，用于 `send_codex.py` / `reach_gpt.py` / `codex_picker_adapter.py`）。
   - 不必三端全装，只用其中任意两端也可独立使用。
3. **Git（可选）**：仅源码克隆与 `make_release.py` 打包需要。

---

## 二、工作根与规则同步

`init_hub.py` 在指定目录下生成最小结构：`.archive-root.json`（哨兵）、`AGENTS.md`、`_state/通道.json`、`cordis.patch.yml` 以及 `_local/` 目录。重复初始化不会覆盖已有文件。

- `python init_hub.py ./my-hub --id team-hub`：为新工作根指定自定义 ID。
- 已有其他工作根指针时，可设 `ARCHIVE_ROOT` 临时覆盖，或用 `python init_hub.py ./my-hub --force` 切换本机绑定。
- `python tools/paths.py --write`：核对当前写根；`python tools/paths.py --set-pointer ./my-hub`：手动绑定已有工作根。
- `python tools/init_rules.py`：把 `rules/core.md` 写入绑定工作根的 `AGENTS.md`；`--target <文件>` 可写入其他客户端的规则文件；`--check` 核对漂移。详见 [安装与校验共享规则](docs/agents-injection.md)。

---

## 三、通道配置与凭据隔离

1. **统一通道配置**：编辑 `<工作根>/_state/通道.json`（或通过 `DSH_CHANNEL_FILE` 环境变量指定路径）。
2. **端口按需修改**：模板中的 `18097` / `18098` 为示例占位端口，请改成你本机实际端口（未设强口令前切勿开启 `allowLan: true`）。
3. **凭据不入仓（`secrets_dir`）**：
   - `通道.json` 中的 `token` 可留空，改填 `token_secret`（如 `harbor_desktop`）；
   - 把真实口令单独放进库外目录 `%LOCALAPPDATA%\agent-archive\secrets\harbor_desktop.txt`（可用 `ARCHIVE_SECRETS_DIR` 覆盖）。

---

## 四、常用环境变量一览

| 环境变量 | 作用 | 默认值 |
| :--- | :--- | :--- |
| `ARCHIVE_ROOT` | 工作根路径 | 无（可由本机指针提供） |
| `ARCHIVE_ID_EXPECTED` | 哨兵校验期望的 `archive_id` | `user-multiagent-hub` |
| `ARCHIVE_SECRETS_DIR` | 库外凭据目录（放 `.txt` 口令文件） | `%LOCALAPPDATA%\agent-archive\secrets` |
| `DSH_CHANNEL_FILE` | 覆盖 `通道.json` 路径 | `<工作根>/_state/通道.json` |
| `CARD_DIR` / `CARD_PORT` | 选窗卡片数据目录与端口 | Windows 优先 `%LOCALAPPDATA%/.multi-ai-collab-kit/cards`，否则 `~/.multi-ai-collab-kit/cards`；端口 `8790` |
| `DELIVERY_HISTORY` | 四态投递历史文件 | 与卡片目录同级的 `投递历史.jsonl` |
| `KIT_CODEX_BIN` / `CODEX_HOME` | Codex 二进制与本机会话存储 | PATH 或 Windows 桌面端默认位置；存储默认 `~/.codex` |
| `PORTA_BASE` | IDE 端本地 Porta 服务地址 | `http://127.0.0.1:3170` |
| `PROTECTED_WORKSPACES` | 受保护工作区关键词（逗号分隔，选窗器强制预排除） | `protected-workspace,private-zone` |
| `FEISHU_LEGACY_SESSION_PREFIX` | 飞书历史会话 ID 前缀约定（可覆盖） | `dfc:` |

---

## 五、挂载 Harbor 插件（DSH 侧）

1. 将 `plugins/dsh-plugin-harbor/` 复制到 DSH 插件加载目录；
2. 参考 `templates/cordis.patch.yml` 在你的 profile 中挂载插件（`name: dsh-plugin-harbor` 必填）；
3. 重启 DSH 后访问 `/api/harbor/health`，再运行 `python tools/channel_params.py --live` 验证目标会话。
