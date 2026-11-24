# 配置选窗卡片

需要 Python 3.10+。默认以桌面右下角弹窗卡片交互，网页版仅作为无桌面环境兜底。

## 一、启动与选窗规范

1. 在工具目录运行 `python tools/card_service.py` 启动桌面卡片服务（也可由脚本调 `python tools/ensure_card_service.py` 自动唤醒）。仅在无桌面环境（如 SSH）或不需要弹窗时加 `--no-popup`。
2. **禁止 AI 主动拉起浏览器**：调用 `tools/send_codex.py --ask` 发起选窗后，桌面右下角会自动弹出分组小卡片，AI 应安静等待用户点击，**不要替用户打开浏览器**。网页地址仅作无桌面环境下的手动兜底。
3. 预演命令：`python tools/send_codex.py --ask --text "选窗试验" --dry-run`。卡片按项目组和「会话区（无项目分类）」分组展示，每个分组底部都附带「＋ 新建窗口并发送」选项（无活跃窗口时也可直接在无项目分类新建）；也可用 `--new --new-cwd <目录>` 直接新建并预演。
4. 先确认正文和目标，之后再用同一入口实际投递。用 `--task` 可保存本次选择，预演不写入任务选择。

## 二、端口与数据目录规范

- **固定只监听本机**：`127.0.0.1`，绝不对局域网或公网暴露。
- **端口三级优先级**（默认 `8790`）：
  1. 环境变量 `CARD_PORT`（最高优先，适合临时测试）；
  2. 工作根 `_state/通道.json` 中的 `"card_port"` 字段（改一次即可让卡片服务与所有发送脚本统一生效）；
  3. 均未配置时默认使用 `8790`。
- **数据目录**：Windows 优先 `%LOCALAPPDATA%/.multi-ai-collab-kit/cards`，其他环境用 `~/.multi-ai-collab-kit/cards`，可用 `CARD_DIR` 覆盖。

`tools/picker.py --card` 可在终端看选项预览。实际选择走 `tools/send_codex.py --ask`，或由已接入的客户端调用 [HTTP 接口](card-service.md)。
