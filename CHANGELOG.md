# 变更记录

## 0.4.0（2026-11-24）

- **公开包瘦身划界**：移除 18 个非通用脚本（重型档案库治理仪式、槽位实验原型、整机备份还原、后台规则监视器、自制更新器及六区路径映射），以及对应测试、非通用中文模板和 `docs/hooks.md`，只保留面向外部用户的通用最小核心。
- **极简工作根与安全寻根**：`init_hub.py` 改为只初始化 `_state/`、`_local/`、最小通用模板与空 Codex 登记册（同时兼容已有旧版登记册，重跑不覆盖）；`paths.py` 新增 `state_path()` / `cache_path()` / `codex_registry_path()`，显式根、空指针或损坏哨兵一律严格抛错，绝不静默回退。
- **Harbor 与 Python 寻根对齐**：`plugins/dsh-plugin-harbor/lib/index.js` 移除旧过渡开关 `write_mode.txt` 依赖，优先读 `ARCHIVE_ROOT`，其次读 `root.txt`。
- **测试全覆盖与默认离线隔离**：修正 3 个独立脚本源码路径，将全部 8 个独立脚本接入 `run_tests.py`；分组卡片测试的活宿主冒烟收敛至 `MACK_HOST_TESTS=1`，默认运行零触碰宿主 Porta 与 Codex 数据库。
- **确定性规则同步与文档精简**：修复 `init_rules.py` 重复同步多写换行问题，实现严格字节幂等；同步精简 `README.md`、`GOVERNANCE.md`、`SETUP.md`、`SANITIZATION.md`、`rules/core.md`、`templates/AGENTS.md` 及 `docs/`。

## 0.3.3（2026-11-24）

- 打包按 Git 的 NUL 分隔清单读取文件名，包含中文模板。发布清单作为 ZIP 外的独立文件，记录实际附件字节数和 SHA-256。
- 初始化按六区结构放置通道、判据与账本，保留自定义档案 ID。规则默认写到绑定档案根，通道检查默认离线。
- 修复 Python 3.10+ 的 pip 安装与版本元数据。补齐传球测试、修正宿主测试开关、关闭规则测试文件句柄。
- Codex 统一入口接入选窗、任务选择、原址回信与中断回合检查。Codex 和 Porta 用四态回执记录实际证据。
- 清理备份与 Git 的本机默认绑定，修复无效计划任务误报。文档补齐路径、参数和卡片接入步骤。

## 0.3.2（2026-11-24）
- 修复：`rules_watch.py` 首次同步时，若状态文件里的摘要已与正本一致（例如误带入维护者的旧状态），新目标会被判“没变”而拿不到规则；现在新目标一律写一次
- 移除：误提交的本机 `rules/.sync-state.json`（已加入 `.gitignore`；它是本机状态，不应随仓库分发）
- 修复：`card_service.py` / `ensure_card_service.py` / `receipt_tri.py` 的默认路径不再写死维护者本机的绝对路径，改用用户目录下的 `.multi-ai-collab-kit`（可用 `CARD_DIR` / `DELIVERY_HISTORY` 环境变量覆盖）
- 修复：`card_service.py` 的保护色关键词去掉内部项目名（普通标题不会再被误标为受保护）
- 修复：`reach_gpt.py` 的 `GPT_DIR` 改为环境变量（不再默认指向维护者的旧目录）
- 修复：`backup_all.py` 遇到本机不存在的来源时跳过而不是拒绝整包；非 Windows／无对应计划任务时不再调 `schtasks`
- 修复：卡片服务端口可用 `CARD_PORT` 环境变量改；卡片数据目录与文档一致（`~/.multi-ai-collab-kit/cards`）
- 文档：`docs/gate-coverage.md` 与仓库实际发送层对齐（去掉未随仓库提供的内部私有脚本引用）
- 文档：插件安装路径、去重表路径、Codex 受保护工作区示例改为通用说法

## 0.3.1（2026-11-24）
- 修复：更新器的 Windows 保留名判断改用标准库接口，Python 3.15 下不再报弃用警告（旧版本行为不变）

## 0.3.0（2026-11-24）
- 新增：规则正本 `rules/core.md` + 幂等安装器 `tools/init_rules.py` + 自动监视 `tools/rules_watch.py`
- 新增：投递回执四态 `tools/receipt_tri.py`（覆盖检查脚本为本机扩展，未随仓库提供）
- 新增：发布与自动更新 `tools/kit_update.py`、`tools/make_release.py`
- 重构：脚本进 `tools/`、测试进 `tests/`、文档进 `docs/`；`run_tests.py` 一条命令跑全量
- 修复：回执模块局部导入导致的崩溃；`guard.py` 不再写死 Git 路径

## 0.2.0（2026-11-23）
- 首版：四层治理甲、选窗卡片、投递闸、Codex 投递约束文档
