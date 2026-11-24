# 配置选窗卡片

需要 Python 3.10+。桌面卡片使用 Tk，网页可在本机浏览器使用。

1. 在工具目录的一个终端运行 `python tools/card_service.py --no-popup`。需要桌面卡片时去掉 `--no-popup`。
2. 默认只监听 `127.0.0.1:8790`。在浏览器打开该地址即可看待选卡片。
3. 在另一终端运行 `python tools/send_codex.py --ask --text "选窗试验" --dry-run`。需要先有能通过检查的本机 Codex 窗口。选择一个标题后，命令会输出正文预览。
4. 先确认正文和目标，之后才用同一入口实际投递。用 `--task` 可保存本次选择，预演不写入任务选择。

Windows 上服务优先使用 `%LOCALAPPDATA%/.multi-ai-collab-kit/cards`，否则使用 `~/.multi-ai-collab-kit/cards`。用 `CARD_DIR` 改存储位置。用 `CARD_PORT` 改端口时，启动和发卡的终端都要设相同的端口。可另用 `python tools/ensure_card_service.py` 启动或检查本机服务。

`tools/picker.py --card` 可在终端看选项预览。实际选择走 `tools/send_codex.py --ask`，或由已接入的客户端调用 [HTTP 接口](card-service.md)。网页可选窗时，Windows 快捷方式自启是可选项。
