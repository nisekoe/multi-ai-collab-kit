# 变更记录

## 0.3.1（2026-11-24）
- 修复：更新器的 Windows 保留名判断改用标准库接口，Python 3.15 下不再报弃用警告（旧版本行为不变）

## 0.3.0（2026-11-24）
- 新增：规则正本 `rules/core.md` + 幂等安装器 `tools/init_rules.py` + 自动监视 `tools/rules_watch.py`
- 新增：投递回执四态 `tools/receipt_tri.py` + 覆盖检查 `tools/check_receipt_coverage.py`
- 新增：发布与自动更新 `tools/kit_update.py`、`tools/make_release.py`
- 重构：脚本进 `tools/`、测试进 `tests/`、文档进 `docs/`；`run_tests.py` 一条命令跑全量
- 修复：回执模块局部导入导致的崩溃；`guard.py` 不再写死 Git 路径

## 0.2.0（2026-11-23）
- 首版：四层治理甲、选窗卡片、投递闸、Codex 投递约束文档
