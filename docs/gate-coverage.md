# 投递闸覆盖范围

| 入口 | 实际检查 |
| --- | --- |
| `tools/send_codex.py` | 核对手动确认、选窗卡片、任务选择或来件回程地址。保存任务选择之后，复用时检查同工作区有无新候选。 |
| `tools/reach_gpt.py` | 真投由统一入口调用。按完整地址复验归档、云端、受保护目录、工作区漂移，检查上一轮状态和已有排队。 |
| `tools/picker.py` | 命令行显示候选与卡片预览。程序的 Codex 提交流程经统一入口复验。 |
| `tools/porta_adapter.py` | 预演和真投均查绑定、身份及工作区，检查信封收件地址。可用 `PORTA_BINDING` 指定此前选中的窗口。 |
| `tools/reach_dsh.py` | 按身份解析并复验选定目标。Harbor 插件执行自己的目标复验。 |

Codex 的 `--confirmed` 表示用户确认了收件窗口。运行 `--dry-run` 会检查目标并显示正文预览。发送、接收日志和机器回执都可用测试夹具单独验证。

## 验收

```bash
python run_tests.py
python tests/test_delivery_entry.py
```

真实 Codex 预演先运行 `python tools/reach_gpt.py --list` 找到目标。用户明确选中后执行以下命令。

```bash
python tools/send_codex.py --thread "<会话号>" --confirmed --text "预演正文" --dry-run
```

要让用户在网页或桌面卡片上选，在另一个终端运行 `python tools/card_service.py`，随后运行 `python tools/send_codex.py --ask --text "预演正文" --dry-run`。

有来件回程地址时，将其整个传给 `--reply-to "codex|local|<会话号>"`。发件地址可以由宿主会话环境提供，也可以用 `--from-addr` 或 `KIT_FROM_ADDR` 指定。

## 回执

- `refused` 有明确拒绝证据。
- `queued` 已有受理证据，仍未核对到正文进入会话。
- `consumed` 已在目标会话里核对到本次正文，回复可能仍在生成。
- `unknown` 超时或缺少证据。机器结果写 `status: null` 和 `resultKnown: false`。

可用 `DELIVERY_HISTORY` 指定记录路径。Codex 的 `--result-file` 还会输出本次机器结果。遇到 `queued` 或 `unknown` 时，先核对原窗口记录再决定是否重试。
