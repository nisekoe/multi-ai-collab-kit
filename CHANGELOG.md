# 变更记录

## 未发布
- 修复：`rules_watch.py` 首次同步时，若状态文件里的摘要已与正本一致（例如误带入维护者的旧状态），新目标会被判“没变”而拿不到规则；现在新目标一律写一次
- 移除：误提交的本机 `rules/.sync-state.json`（已加入 `.gitignore`；它是本机状态，不应随仓库分发）
- 修复：`card_service.py` / `ensure_card_service.py` / `receipt_tri.py` 的默认路径不再写死维护者本机的 `F:\hub`，改用用户目录下的 `.multi-ai-collab-kit`（可用 `CARD_DIR` / `DELIVERY_HISTORY` 环境变量覆盖）
- 文档：`docs/gate-coverage.md` 与仓库实际发送层对齐（去掉未随仓库提供的 `delivery_gate` / `route_policy` / `reach_momo` / `push_dsh_ningning` 引用）

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
