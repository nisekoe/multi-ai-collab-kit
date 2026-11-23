# 投递闸的覆盖面（哪些入口会被拦、哪些还靠自觉）

> 闸不是一处检查，而是「所有真正发消息的路径都要经过它」。这份文档列清楚：哪些已经硬拦，哪些还只是建议，以及唯一的放行口。

## 一、设计原则

- **闸放在真正发消息的那一层**：不管从哪调过来，只要最终落到发送函数，就要过闸。
- **上层已判定时不许二次判定**：统一入口（`send_codex.py`）判定后，调用下层发送器要带 `--skip-gate`，否则会被自己的闸再拦一次。
- **放行口只有一个**：`--skip-gate` 或环境变量 `DAFEIYU_GATE=0`；只允许统一入口在完成判定后使用，其他脚本不许用。

## 二、覆盖矩阵

| 入口 | 是否过闸 | 怎么覆盖 |
|---|---|---|
| `send_codex.py`（统一入口） | 是 | 自己先跑 `delivery_gate` + `route_policy`，判定 deliver/queue 才投 |
| `reach_gpt.py`（真发送层） | 是 | 发送前再跑一次 `delivery_gate` + `route_policy`；被拒返回 6，要确认返回 4 |
| `picker.py` → Codex | 是 | 它通过 `reach_gpt.py` 提交，闸在发送层生效 |
| `picker.py` → Porta（猫猫） | 部分 | 走 `porta_adapter`；绑定是否过期仍要靠 `delivery_gate --porta --binding` 主动查，未强制 |
| `reach_momo.py` / `push_dsh_ningning.py` | 部分 | 有「按身份解析、多候选拒绝」的保护，也有回执记录；没有 Codex 那套归档/保护/写者检查（这两条腿没有这些状态） |
| 手工在 Python 里 import 发送函数 | 视实现 | 只要最终调到 `reach_gpt` 就过闸；直接拼 HTTP 的则绕过 |

## 三、已知缺口

1. **Porta 腿的绑定新鲜度**：闸只能给建议（列出比绑定更新的会话），要不要拒由调用方决定。原因是「新窗口」有时正是你想投的目标，硬拒会误伤。
2. **直连 HTTP**：任何自己拼 `POST /api/harbor/say` 的脚本都在闸外；这类脚本应改成调 `reach_momo.py` / `push_dsh_ningning.py`。
3. **`DAFEIYU_GATE=0` 是全局开关**：设了它等于关闸，只建议在离线测试里用。

## 四、怎么验证闸真的生效

```bash
# 归档线：应拒绝，退出码 6
python tools/reach_gpt.py --thread <已归档UUID> --text probe --dry-run

# 正常线：应通过（dry-run 不真投）
python tools/reach_gpt.py --thread <正常UUID> --text probe --dry-run

# 统一入口：先判后投
python send_codex.py --thread <UUID> --file 正文.txt
```

退出码：0 通过；4 需要人确认；6 硬拒。

依据：本机 2026-11-24 实测（归档线拒、正常线通、picker 经 reach_gpt 提交）；Porta 侧为设计约定，未做硬拦验证。