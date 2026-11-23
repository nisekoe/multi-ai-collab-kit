# 把规则贴进你的 AI（三种客户端）

> 正本：`rules/core.md`；安装器：`tools/init_rules.py`。

> 规则正本是 `rules/core.md`。改完正本跑一次安装器，它会往目标文件里写一段**带标记**的文本；重复跑不会重复写，`--check` 能查漂移。

## 一、先看你要贴给谁

| 客户端 | 贴到哪 | 说明 |
|---|---|---|
| DSH（桌面/工程） | 工作区根的 `AGENTS.md` | 每轮自动注入；建议直接指向这个仓库 |
| Codex（CLI / 桌面） | 你的 Codex 全局 `AGENTS.md`，或项目根的 `AGENTS.md` | 全局文件对所有会话生效，项目文件只对该目录生效 |
| Antigravity（IDE） | 项目里的 `AGENTS.md` 或它自带的规则入口 | 多数版本会读项目根的同名文件 |

## 二、三步贴上

```bash
python tools/init_rules.py --target AGENTS.md          # 写进去
python tools/init_rules.py --target AGENTS.md --check   # 核对有没有漂移
```

第三件：把 `docs/card-service.md` 里的卡片服务按 `docs/setup-card-service.md` 起起来——规则里的“需要人选窗口时走卡片”才有东西可走。

## 三、以后规则变了怎么办

1. 改 `rules/core.md`；
2. 重新跑一次安装器（同一个命令，会替换旧段落）；
3. `--check` 显示“与正本一致”即可。

不要手改目标文件里那一段——下次安装会覆盖；要加本地补充，写在标记段落**之外**。

## 四、撤掉

```bash
python tools/init_rules.py --target AGENTS.md --remove
```

只删标记段落，不动文件里的其他内容。