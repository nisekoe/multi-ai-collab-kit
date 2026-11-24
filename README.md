# multi-ai-collab-kit

> 本地多 AI 跨平台协作的**通用最小核心**（实测跑通桌面端 DSH / IDE Antigravity / CLI Codex 三端互通，可按规范扩展接入更多平台）。

让本机几个**已经开着的**异构 AI 窗口互相发消息、选窗确认、核验送达的一套轻量机制工具与配置模板：**可视化选窗防误投 ＋ 三端活窗投递 ＋ ORIGIN 信任边界 ＋ 四态回执 ＋ 确定性安装与规则同步**。

> 跨 Coding Agent 互联社区已有不少优秀开源先例（如 [cospan](https://github.com/keli-wen/cospan)、[dsh-baton](https://github.com/JialaoLiu/dsh-baton) 等，均 MIT）。本仓库聚焦贴合日常桌面现成窗口习惯的通用最小内核，不含绑定单一作者习惯的重型工作流。

---

## 快速开始

下载 [Release 附件](https://github.com/nisekoe/multi-ai-collab-kit/releases) 并解压，或从 Git 克隆。使用 Python 3.10+，在工具目录执行：

```bash
python run_tests.py
python init_hub.py ./my-hub
python tools/init_rules.py --root ./my-hub
python tools/channel_params.py
```

`init_hub.py` 会在 `./my-hub` 下创建最小运行目录（`_state/`、`_local/`）、通用通道配置模板 `_state/通道.json` 和 `AGENTS.md`；`init_rules.py` 将核心协作纪律幂等写入 `AGENTS.md`；`channel_params.py` 离线校验通道配置。

也可在虚拟环境安装为命令行工具：`python -m pip install .`，之后可从任意目录运行 `mack-init-hub` 和 `mack-init-rules`。

---

## 一、核心能力（通用最小核心）

1. **可视化选窗与防误投**
   - 本机常驻轻量卡片服务 `tools/card_service.py`（默认端口 `8790`），在存在多个候选或新窗口时由用户点选确认，绝不按“最近活跃窗口”盲猜。
   - Codex 投递统一走 `tools/send_codex.py`：支持现场选窗（`--ask`）、复用任务选择（`--task`）和按完整回程地址原路回复；配置与预演见 [卡片服务接入](docs/setup-card-service.md)。
   - 已归档会话、受保护工作区、选定后目录漂移的目标一律拒投；目标正忙或上一轮中断时不盲目排队。详见 [投递闸覆盖范围](docs/gate-coverage.md)。
2. **三端活窗桥接**
   - **Codex CLI**：`send_codex.py` / `reach_gpt.py` / `codex_picker_adapter.py` / `route_policy.py`
   - **IDE Antigravity（Porta）**：`porta_adapter.py`
   - **桌面端 DSH（Harbor）**：`reach_dsh.py` / `channel_params.py` / `session_identity.py` / `delivery_receipt.py` ＋ 宿主插件 [plugins/dsh-plugin-harbor](plugins/dsh-plugin-harbor)
   - 不必三端全装，只用其中任意两端也可独立工作。
3. **ORIGIN 信任边界与消息信封**
   - `origin_envelope.py` / `msg_envelope.py` 为跨 Agent 消息加上去重信封与首行信任标签：
     ```text
     [ORIGIN peer_agent=planner_bot | trust=peer_only | no_irreversible_approval]
     ```
   - 明确声明：**同事 AI 的请求绝不等于用户本人对删除、公网外发、付费等不可逆操作的批准**。
4. **四态回执（以证据为准）**
   - `receipt_tri.py` 将投递结果严格区分为 `refused`、`queued`、`consumed`、`unknown`，只有正文进入目标会话的证据才算送达，不把“退出码 0”冒充送达。
5. **确定性安装与规则校验**
   - `paths.py` 提供零硬编码寻根与库外凭据隔离（`secrets_dir`）；`init_rules.py` 幂等同步 `rules/core.md` 并支持 `--check` 防漂移校验。

---

## 二、仓库目录一览

| 目录 / 文件 | 用途 |
| :--- | :--- |
| `init_hub.py` / `tools/paths.py` / `tools/safe_write.py` | 极简工作根初始化、零硬编码寻根与库外凭据隔离、原子安全写入 |
| `rules/core.md` / `tools/init_rules.py` | 通用协作规则正本与确定性幂等安装/校验器 |
| `tools/card_service.py` / `tools/ask_card.py` / `tools/ensure_card_service.py` | 本机可视化选窗卡片服务与客户端 |
| `tools/picker.py` / `tools/window_picker.py` | 跨三端统一候选聚合与选窗编排 |
| `tools/send_codex.py` / `tools/reach_gpt.py` / `tools/codex_picker_adapter.py` / `tools/route_policy.py` | Codex 统一发送入口、候选发现、路由判定与发送层 |
| `tools/porta_adapter.py` | Antigravity Porta 桥接适配器 |
| `tools/reach_dsh.py` / `tools/channel_params.py` / `tools/session_identity.py` / `tools/delivery_receipt.py` | DSH 桥接腿、通道参数唯一解析、会话身份判别与回执 |
| `tools/msg_envelope.py` / `tools/origin_envelope.py` / `tools/receipt_tri.py` | 消息去重信封、ORIGIN 信任头与四态回执 |
| `tools/make_release.py` | 维护者按 Git 跟踪清单打 ZIP 包并生成 SHA-256 清单 |
| `plugins/dsh-plugin-harbor/` | DSH 宿主端港湾插件（Node.js，活会话枚举、归档闸与队列投递） |
| `templates/` | 最小通用模板（`AGENTS.md`、`通道.json`、`cordis.patch.yml`、`.archive-root.json`） |
| `docs/` | 接入与机制文档（卡片服务、Codex 投递、投递闸覆盖、规则安装、节流、打包发布） |
| `tests/` / `run_tests.py` | 离线隔离单元测试套件（一条命令跑全量） |
| `SETUP.md` / `GOVERNANCE.md` / `SANITIZATION.md` | 环境接入指南、协作边界说明与脱敏说明 |

---

## 三、系统局限性与诚实边界

在正式体验前，请明确以下边界：

1. **需自备宿主客户端与账号**：本仓库只提供本地桥接脚本、插件源码与配置模板，不包含任何大模型账号、API Key 或客户端本体（详见 [SETUP.md](SETUP.md)）。
2. **平台兼容性边界**：当前实测覆盖 DSH / Antigravity / Codex 三端，尚未对 Claude 平台完成端到端集成测试。
3. **非“一键全自动编排”**：本工具不内置 Token 成本核算或重型多 Agent 编排引擎，强调把选窗确认权和不可逆操作批准权牢牢留在用户手里。
4. **脱敏保证**：全仓零个人信息、零本机硬编码路径、零真实凭据、零内部角色名，详见 [SANITIZATION.md](SANITIZATION.md)。

## 许可

MIT。详见 [LICENSE](LICENSE)。
