# 脱敏说明（SANITIZATION）

本仓库是「本地多 AI 跨平台活窗协作」实践的**机制层与通用工具**公开包，发布前经过三轮脱敏与 P2 隐私硬门自动化审查。

## 一、包含什么

- **核心机制与寻根**：`paths.py`、`archive_paths.py`、`safe_write.py`、`origin_envelope.py`、`msg_envelope.py`、`handoff_lint.py`、`pass_gate.py`、`comm_gate.py`、`plan_gate.py`、`guard.py`。
- **跨平台桥接与插件**：`channel_params.py`、`session_identity.py`、`delivery_receipt.py`、`reach_dsh.py`、`porta_adapter.py`、`reach_gpt.py`、`codex_picker_adapter.py`、`plugins/dsh-plugin-harbor/`。
- **统一选窗器与智能调度层 v0.6**：`picker.py`、`window_picker.py`、`project_slots.py`、`picker_slots_orchestrator.py`、`picker_ranker.py`、`card_renderer_v2.py`、`stage3_lifecycle_handoff.py`。
- **开箱即用模板与示例**：`templates/`（6 套配置模板）、`examples/`（中性虚构示例 `planner_bot / review_bot / coder_bot` 与虚构项目 `demo-alpha`）、`test_*.py` 隔离离线单测。

## 二、不包含什么（P2 硬门，零容忍）

- 个人信息、真实姓名、账号、QQ/邮箱等个人标识。
- 内部角色昵称与成员关系（所有代码、schema 字段、注释与测试均已统一为 `desktop_bot / feishu_bot / review_bot / coder_bot / planner_bot / user` 等中性标识）。
- 本机绝对路径、盘符私有路径、真实用户名目录、真实业务工作区名称。
- 任何真实凭据、Token、密钥、内网真实 IP 与内部真实监听端口。
- 内部档案库正文、人格正本、记忆库、内部脚本名与工单号。
- 原始未脱敏录屏母带（母带严格本地隔离，绝不入仓、绝不外发）。

## 三、披露分层

1. **机制完整公开**：协议、信任边界、选窗防误投、CAS 槽位与门禁逻辑全部公开可复现。
2. **脱敏工具与模板可给**：通用化、环境变量参数化后的脚本、插件与模板完整发布。
3. **私人环境与业务细节不展开**：真实部署拓扑、私人路径、受保护业务目录与人格设定只讲原则、不入仓。

## 四、技术默认约定说明（非个人数据）

- **`dfc:` 会话前缀**：属于飞书集成通道的历史会话 ID 技术前缀约定，并非个人隐私数据；现已参数化为环境变量 `FEISHU_LEGACY_SESSION_PREFIX`（及 `预设登记模板.json` 中的 `legacy_feishu_prefix` 字段，默认 `dfc:`），用户可按自身通道约定覆盖。
- **示例端口与受保护工作区**：配置模板中的 `18097` / `18098` 与 `protected-workspace` / `private-zone` 均为通用占位示例，实际使用时通过 `通道.json` 和 `PROTECTED_WORKSPACES` 环境变量自行配置。
- **公开协议与标准术语**：如 `file://` URI、MCP、HTTP 状态码、HMAC-SHA256、CAS 等属通用技术名词，不视为泄露。

## 五、审查记录

- 自动化静态扫描（覆盖正/反斜杠路径、中英文内部人格子串、私有目录名、私人标识、硬编码端口与凭据）：**0 命中**。
- 全量 Python 编译检查（`py_compile`）：**0 失败**。
- 隔离临时工作区（`ARCHIVE_ROOT` 指向临时目录）离线测试套件验证通过，零触碰真实档案库、零真实投递。
