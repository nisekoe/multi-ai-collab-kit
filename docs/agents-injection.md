# 安装与校验共享规则

规则正本在 `rules/core.md`。初始化工作根后，在工具包目录执行：

```bash
python tools/init_rules.py
python tools/init_rules.py --check
```

默认写入当前绑定工作根的 `AGENTS.md`。若要指定其他工作根可用 `--root <路径>`；若要直接写入某个客户端的规则文件，可用 `--target <文件>`。

## 更新与防漂移

修改 `rules/core.md` 后，重新运行 `python tools/init_rules.py`（加对应 `--root` 或 `--target`）即可幂等刷新标记段落，再用 `--check` 核对是否一致。

- 请勿手动修改目标文件里的规则标记段落；
- 你的本地补充说明写在标记段落之外即可，重复同步不会覆盖段落外的内容。

## 撤销

```bash
python tools/init_rules.py --remove
```

移除规则文件中的标记段落（指定过 `--target` 时请带上同一参数）。
