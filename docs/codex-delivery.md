# Codex 投递方式

`tools/send_codex.py` 选择目标并把它交给 `tools/reach_gpt.py` 复验。先运行 `python tools/reach_gpt.py --list` 查看本机候选，或者按 [卡片说明](setup-card-service.md) 启动本机选窗服务。

- `--ask` 由用户在卡片上选。`--thread <会话号> --confirmed` 用于用户已点名的窗口。
- 有来件回程地址时用 `--reply-to "codex|local|<会话号>"` 原址回复。
- `--mode queue` 交给 Codex 队列消费。上一轮中断时会拒绝继续入队。已有待处理项时先等对方处理。
- `--mode sync` 通过 Codex CLI 同步续跑。窗口忙时先等待。遇到已有写者冲突，保留原回执供核对。
- `--dry-run` 复验并预览正文。`--file <文件>` 可读取 UTF-8 长文。

真实投递需要支持相应命令的 Codex CLI。工具优先读取 `KIT_CODEX_BIN`，其次从 PATH 或 Windows 桌面安装定位。只有本机可核实的窗口能用此桥接方式。具体的目标检查与回执见 [投递闸覆盖范围](gate-coverage.md)。
