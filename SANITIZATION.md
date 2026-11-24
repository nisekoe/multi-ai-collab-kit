# 脱敏说明（SANITIZATION）

本仓库是「本地多 AI 跨平台活窗协作」的**通用最小核心工具与配置模板**公开包，发布前经过脱敏与隐私硬门自动化审查。

## 一、包含什么

- **核心寻根与安全机制**：`init_hub.py`、`paths.py`、`safe_write.py`、`init_rules.py`、`origin_envelope.py`、`msg_envelope.py`、`receipt_tri.py`、`make_release.py`。
- **可视化选窗与三端桥接**：`card_service.py`、`ask_card.py`、`ensure_card_service.py`、`picker.py`、`window_picker.py`、`send_codex.py`、`reach_gpt.py`、`codex_picker_adapter.py`、`route_policy.py`、`porta_adapter.py`、`reach_dsh.py`、`channel_params.py`、`session_identity.py`、`delivery_receipt.py`、`plugins/dsh-plugin-harbor/`。
- **开箱即用模板与测试**：`templates/`（最小通用模板：`AGENTS.md`、`通道.json`、`cordis.patch.yml`、`.archive-root.json`）、`examples/`（中性虚构示例 `planner_bot / review_bot / coder_bot`）、`tests/` 隔离离线单测。

## 二、不包含什么（隐私硬门，零容忍）

- 个人信息、真实姓名、账号、QQ/邮箱等个人标识。
- 内部角色昵称与成员关系（所有代码、schema 字段、注释与测试均已统一为 `desktop_bot / feishu_bot / review_bot / coder_bot / planner_bot / user` 等中性标识）。
- 本机绝对路径、盘符私有路径、真实用户名目录、真实业务工作区名称。
- 任何真实凭据、Token、密钥、内网真实 IP 与内部真实监听端口。
- 内部档案库正文、人格正本、记忆库、私人工作流脚本。

## 三、技术默认约定说明（非个人数据）

- **`dfc:` 会话前缀**：属于飞书集成通道的历史会话 ID 技术前缀约定，并非个人隐私数据；现已参数化为环境变量 `FEISHU_LEGACY_SESSION_PREFIX`（默认 `dfc:`），用户可按自身通道约定覆盖。
- **示例端口与受保护工作区**：配置模板中的 `18097` / `18098` 与 `protected-workspace` / `private-zone` 均为通用占位示例，实际使用时通过 `通道.json` 和 `PROTECTED_WORKSPACES` 环境变量自行配置。
- **公开协议与标准术语**：如 `file://` URI、MCP、HTTP 状态码、SHA-256 等属通用技术名词，不视为泄露。

## 四、审查记录

- 自动化静态扫描（覆盖盘符绝对路径、中英文内部人格子串、私有目录名、硬编码端口与凭据）：**0 命中**。
- 全量 Python 编译检查（`py_compile`）：**0 失败**。
- 隔离临时工作区（`ARCHIVE_ROOT` 指向临时目录）离线测试套件全量通过，零触碰真实工作区、零真实投递。
