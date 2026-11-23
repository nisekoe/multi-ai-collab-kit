# 把门禁下沉成客户端 Hook（示例）

> 第四层纪律（提示词）靠自觉；能挂 Hook 的客户端，可以把最关键的两条变成硬拦截。这里给一套最小示例，按你所用客户端的 Hook 规范改字段名即可。

## 一、拦什么

1. **写档案根之外的路径**：任何写入落到 `90-archive`、`10-governance` 之外，直接拒。
2. **绕过工具直写账本**：目标文件是 `工作台结果.md` 且不是经由 `ledger_lock.py`，直接拒。

## 二、Hook 契约（通用形状）

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "write_file|edit_file",
        "command": "python .hooks/guard_write.py",
        "timeout_ms": 5000
      }
    ]
  }
}
```

Hook 进程从 stdin 读一个 JSON（含工具名与参数），向 stdout 写一个 JSON：

```json
{ "decision": "block", "reason": "账本只能经 ledger_lock.py 写" }
```

`decision` 取 `allow` / `block`；`block` 时把 `reason` 回给模型。

## 三、参考实现（`.hooks/guard_write.py`）

```python
import json, os, sys
from pathlib import Path

ROOT = Path(os.environ.get("ARCHIVE_ROOT", ".")).resolve()
LEDGER = "工作台结果.md"

def main():
    ev = json.load(sys.stdin)
    args = ev.get("tool_input") or ev.get("arguments") or {}
    path = args.get("file_path") or args.get("path") or ""
    if not path:
        return {"decision": "allow"}
    p = Path(path).resolve()
    if LEDGER in p.name and "ledger_lock" not in json.dumps(ev):
        return {"decision": "block", "reason": "账本只能经 ledger_lock.py 写"}
    if not str(p).startswith(str(ROOT)):
        return {"decision": "block", "reason": f"写入越出档案根：{p}"}
    return {"decision": "allow"}

if __name__ == "__main__":
    print(json.dumps(main()))
```

## 四、注意

- Hook 只是**客户端侧**的门；换一个不支持 Hook 的客户端就失效，所以第四层纪律仍要保留。
- 拦截要有逃生口：人明确要求时允许一次性放行，否则会卡住正当操作。
- Hook 失败（超时、脚本报错）应默认 allow 并记日志，不要让它变成新的故障点。