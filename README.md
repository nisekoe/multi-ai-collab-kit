# multi-ai-collab-kit

> 本地多 AI 跨平台协作工具包与治理规范（当前实测跑通桌面端 DSH / IDE Antigravity / CLI Codex 三端互通，可按规范扩展接入更多平台）。

让本机几个**已经开着的**异构 AI 窗口像同桌同事一样互相派活、接棒、核验的一套**机制层工具与本地工程秩序**。

> 跨 Coding Agent 互联社区已有不少成型框架（如由主 AI 统筹指挥、精致控制 Token 的编排方案，以及 [cospan](https://github.com/keli-wen/cospan)、[dsh-baton](https://github.com/JialaoLiu/dsh-baton) 等优秀开源先例，均 MIT）。本仓库分享的是贴合日常桌面现成窗口习惯的一种实践解法：**outbound 活窗注入 ＋ 统一选窗防误投 ＋ 四层治理叠甲的本地工程秩序**。

---

## 选窗与投递

- Codex 发送统一走 `tools/send_codex.py`。可以现场选窗、复用任务选择或按完整回程地址回复。
- 选窗卡片使用本机 `tools/card_service.py`。配置和预演见 [卡片服务接入](docs/setup-card-service.md)。
- 回执保存为 `refused`、`queued`、`consumed` 或 `unknown`。只有正文进入目标会话的证据才算送达。检查项目见 [投递闸覆盖范围](docs/gate-coverage.md)。

## 快速开始

下载 [Release 附件](https://github.com/nisekoe/multi-ai-collab-kit/releases)并解压，或从 Git 克隆。使用 Python 3.10+，在工具目录执行以下命令。

```bash
python run_tests.py
python init_hub.py ./my-hub
python tools/init_rules.py --root ./my-hub
python tools/channel_params.py
```

新档案根里已生成六区、配置模板、账本和角色临时本子。最后一条只检查配置，可以在没有客户端、账号和口令时执行。若已有其他档案根的指针，按 [SETUP.md](SETUP.md) 设置当前目录所用的根。

## 一、系统局限性与免责声明（四项边界）

在正式体验本工具前，请从客观与严谨的角度明确以下四项边界条件：

1. **平台兼容性边界**：本工具尚未接入 Claude 平台完成端到端集成测试，因此在当前阶段无法对其跨平台协作的稳定性作出任何形式的保证。
2. **主导角色举证边界**：鉴于 GPT 的自然语言输出风格在「说人话」维度上存在一定的交互摩擦，本框架从未实际测试由 GPT 牵头主导项目；但基于严谨的举证原则，目前亦不存在任何实证能够证明 GPT 无法胜任主导位置。
3. **架构成熟度与成本边界**：说到底，本方案本质上是由 AI 小白构建的早期不成熟原型，在设计阶段完全未纳入 Token 消耗核算与精细化调度策略，因而既无法保证高执行效率，也不能承诺任务规划的精准度。
4. **任务量级与适用规模边界**：需要坦诚界定的是，虽然作者已通过该项目「完成多项大型任务」，终究是基于作者这一 AI 小白自身视角所定义的相对量级；在真正意义上的重大型工程与复杂编程场景下，本协作框架并不保证能够产生实质性的能力强化作用。

归根结底，算力账单、协作稳定性、规划精度与工程规模之间的权衡，仍需结合你的实际环境自行评估。

---

## 二、我们做了哪些努力实现功能（四层治理架构）⭐ 请先读这一节

**这套包真正的价值，不是「几个能发消息的脚本」，而是脚本背后叠起来的四层甲。** 多 AI 跨窗口协作要稳，靠的不是某一个工具，是这四层一起兜住——判据决定何时停、何时交；脚本把规矩变成强制动作，不靠自觉；指针让东西各就各位、零硬编码寻根；共用规则让 AI 每轮开工都知道要守什么。**请先看懂这一节，再往下读怎么装。**

> **举个叠甲的例子**：选窗投递不只是「找到窗口就发」——已归档的会话一律拒投；旧绑定失效时先剔除归档、再检测新活窗，唯一且工作目录相符才自动切换并告知，有歧义就列出候选找人确认，绝不静默直投。判据＋脚本＋插件三处一起把关，这就是四层甲叠在一起的样子。

| 层级 | 核心职责 | 包内对应模块 / 资产 | 状态与你的操作 |
| :--- | :--- | :--- | :--- |
| **第一层·判据层** | 什么时候停、什么时候交 | `guard.py`（可逆判据＋三红线）、`pass_gate.py`＋`传球判据.json`（传球门五问）、`comm_gate.py`（节奏闸防踩踏）、`plan_gate.py`（方案门禁） | ✅ **已含**：脚本与判据模板齐备 |
| **第二层·脚本层** | 把规矩变成强制动作，不靠自觉 | `safe_write.py`（唯一文本写口）、`ledger_lock.py`（账本锁）、`handoff_lint.py`（四字段门禁）、`closeout.py`（收工唯一入口）、`sync_check.py`、`origin_envelope.py`、`delivery_receipt.py`、`picker*.py`、三桥接腿、`backup_all.py` | ✅ **已含**：机制脚本与隔离测试 |
| **第三层·指针层** | 东西放哪、零硬编码寻根 | `paths.py`（4 级寻根）、`archive_paths.py`（六区映射）、`.archive-root.json`（哨兵）、`入口.md`（收工与开工总指针）、`通道.json` 等配置正本 | ⚙️ **模板已含**：需按 Quickstart 在你的工作根目录建好六区骨架与 `入口.md` |
| **第四层·共用规则层** | AI 每轮开工要读的纪律与注入指令 | 五条硬约束、`AGENTS.md`（每轮注入纪律＋开工先跑 `comm_gate`）、三红线/四字段/单写者/收尾铁律 | 🛠️ **需按你环境立起**：提供 `templates/AGENTS.md` 与 [GOVERNANCE.md](GOVERNANCE.md) 供你与 AI 助手裁剪注入 |

> **诚实提醒（必读）**：
> - **协作不可能不经亲身体验与调整就一键完美配好**——各家电脑环境、所用的客户端与协作框架本就不同。我们叠好自家的甲，并把「加了哪些判据、脚本、指针与共用规则」讲清楚；你怎么叠、怎么调，由你和你的 AI 助手商量着来。
> - 本仓库把第 1–3 层机制脚本、桥接插件与配置模板全部给齐，并在 [GOVERNANCE.md](GOVERNANCE.md) 讲清四层甲的完整剖析；**第 4 层共用规则正文与 `入口.md` / `AGENTS.md` 指针需要你参照模板在本地立起来**（尤其注意：`closeout.py` 收工对账会读取 `<ARCHIVE_ROOT>/入口.md`，请务必按下方 Quickstart 复制就位）。
> - 我们**不承诺「装上即完美跑通」**，承诺的是**「机制与秩序完整可查」**。如何与你当前环境兼容，请务必与你的 AI 助手充分讨论后自行取舍。

---

## 三、它解决什么

同时开着多个 AI 窗口干活时，最累的是人当「复制粘贴传话筒」。这套工具包把跨平台传话做成可控的本地流水线，并守住四条底线：

1. **话怎么递进去**：把消息主动注入对方**正在跑的活会话**（三条桥接腿：`reach_dsh.py` / `porta_adapter.py` / `reach_gpt.py` ＋ 宿主插件 `plugins/dsh-plugin-harbor`）。
2. **递给谁（绝不猜投，含归档闸）**：统一选窗器与智能调度层（`picker.py` / `window_picker.py` / `project_slots.py` / `picker_slots_orchestrator.py`）在无绑定或歧义时强制出卡由人确认；目标正忙（`BUSY`）绝不漂移改投；受保护工作区在打分与卡片中彻底预排除。**已归档会话一律拒投**（`channel_params.py` / `reach_dsh.py` ＋ 港湾插件归档闸，三层把关）；旧绑定窗失效时先剔除归档、再检测新活候选——唯一且工作目录相符才自动切换并明确告知，多候选或无候选则列出候选找人确认，绝不静默直投旧绑定。
3. **身份与信任边界（ORIGIN 信任头）**：每条跨 Agent 消息首行必带：
   ```text
   [ORIGIN peer_agent=planner_bot | trust=peer_only | no_irreversible_approval]
   ```
   含义：**同事 AI 的请求绝不等于用户本人对删除 / 外发 / 付费等不可逆操作的批准**（对齐 [Claude Code Agent Teams 官方文档](https://code.claude.com/docs/en/agent-teams) 的信任边界）。
4. **交接与收尾有证据**：交接四字段门禁（`handoff_lint.py`）、传球门（`pass_gate.py`）、单写者原子落盘（`safe_write.py`）、换窗接棒与结项回收（`stage3_lifecycle_handoff.py`）。

---

## 四、接入你使用的客户端

1. `python tools/paths.py --write` 显示当前绑定的档案根。`python tools/pass_gate.py --demo` 可检查刚装好的判据。
2. 编辑档案根下的 `_state/通道.json`，把示例端口换成实际端口。凭据放到库外目录，见 [环境接入指南](SETUP.md)。
3. 将档案根的 `AGENTS.md` 配置到实际使用的客户端。用 `python tools/init_rules.py --check` 核对其规则。需要同步别处的文件时，用 `--target` 指定实际注入文件。
4. 先验证本机配置和离线用例。接好宿主后，再按 [卡片接入指南](docs/setup-card-service.md) 运行选窗预演。Codex 预演会复验目标并显示加好信封的正文。

也可在虚拟环境安装 Python 工具。`python -m pip install .` 完成后，可以从其他目录运行 `mack-init-hub` 和 `mack-init-rules`。Harbor 插件使用源码仓库或 Release 附件中的 `plugins/`。

---

## 五、仓库目录一览

| 目录 / 文件 | 用途 |
| :--- | :--- |
| `GOVERNANCE.md` | **四层甲治理架构与共用纪律完整说明**（判据层 / 脚本层 / 指针层 / 共用规则层） |
| `paths.py` / `archive_paths.py` / `safe_write.py` | 4 级寻根契约（`ARCHIVE_ROOT`）、六区路径映射、哨兵校验、原子安全写入 |
| `origin_envelope.py` / `msg_envelope.py` | `ORIGIN` 信任头与去重信封（`task / msg / reply` ID） |
| `channel_params.py` / `session_identity.py` / `delivery_receipt.py` | 通道参数唯一解析、会话身份判别、投递回执账本 |
| `reach_dsh.py` / `porta_adapter.py` / `reach_gpt.py` / `codex_picker_adapter.py` | 三条跨平台桥接腿（DSH / Antigravity Porta / Codex） |
| `picker.py` / `window_picker.py` / `project_slots.py` / `picker_slots_orchestrator.py` / `picker_ranker.py` / `card_renderer_v2.py` / `stage3_lifecycle_handoff.py` | 统一选窗器与智能调度层 v0.6（槽位账本、确定性打分、路 A 卡片、换窗接棒与收尾回收） |
| `handoff_lint.py` / `pass_gate.py` / `comm_gate.py` / `plan_gate.py` / `guard.py` / `closeout.py` / `sync_check.py` / `ledger_lock.py` | 协作治理判据与强制门禁（交接四字段、传球门、节奏闸、一页纸门禁、可逆守卫、收工对账、账本锁） |
| `plugins/dsh-plugin-harbor/` | DSH 宿主端港湾插件（Node.js，负责活会话枚举、身份复验与队列投递） |
| `templates/` | 配置与纪律模板（`入口.md`、`AGENTS.md`、哨兵、通道、传球判据、窗口登记、预设登记、插件 patch） |
| `examples/` | 虚构中性角色（`planner_bot / review_bot / coder_bot`）示例消息 |
| `test_*.py` | 离线隔离单元测试套件 |
| `SETUP.md` / `SANITIZATION.md` | 环境接入指南与脱敏硬门说明 |

---

## 六、开源定位与脱敏说明

- **开源先例与定位**：单次异步委派 / 跨窗接力可参考 [cospan](https://github.com/keli-wen/cospan) 或 [dsh-baton](https://github.com/JialaoLiu/dsh-baton)（本仓库回执带证据思路参考自 baton）。
- **需要自备宿主客户端与账号**：本仓库提供桥接胶水、选窗调度与四层治理规范，不包含任何大模型账号、API Key 或客户端本体（详见 [SETUP.md](SETUP.md)）。
- **脱敏硬门**：本仓库全部代码、模板与示例已通过 P2 脱敏扫描（零个人信息、零本机路径、零凭据、零内部角色名）。详见 [SANITIZATION.md](SANITIZATION.md)。

## 许可

MIT。详见 [LICENSE](LICENSE)。
