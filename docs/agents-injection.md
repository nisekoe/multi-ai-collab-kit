# 安装共享规则

规则正文在 `rules/core.md`。初始化档案根后，在工具目录执行以下命令。

```bash
python tools/init_rules.py
python tools/init_rules.py --check
```

默认写入绑定档案根的 `AGENTS.md`。指定另一个档案根用 `--root <档案根>`，向实际客户端注入文件写入可用 `--target <文件>`。将该文件配置到客户端工作区或客户端自己的规则入口。

## 同步变化

正本改动后可重跑安装器。需要持续监视时，请把示例路径换成实际文件。

```bash
python tools/rules_watch.py --watch --target "./my-hub/AGENTS.md"
```

正本变更会同步。只改目标里的标记块时，监视器会报告漂移。确认要恢复时执行 `python tools/init_rules.py --target "./my-hub/AGENTS.md"`，然后加 `--check` 核验。自己的补充写在标记块外。

## 撤销

`python tools/init_rules.py --remove` 移除绑定根规则文件里的标记块。指定过其他目标时带上同一个 `--target`。
