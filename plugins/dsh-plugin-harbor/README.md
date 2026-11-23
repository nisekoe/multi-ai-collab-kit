# 港湾（dsh-plugin-harbor）

> 结论一句话：**给飞书助手一个投递口，把一句话投进指定 DSH 会话**——跑在宿主进程内部，直接调 `sessionController.prompt()`，所以不用 token、不碰 mux 协议、也不会被 `session/writer-held` 挡（那道闸挡的是"第二个宿主"，不是宿主自己）。
> 依据：`dsh-api-session-controller/lib/types/types.d.ts` 里的 `SessionPromptRequest`（`requestId`/`sessionId`/`mode`/`content`）与 `prompt()` 签名；`dsh-host-webserver/lib/types/index.d.ts` 里的 `WebRoute { kind, path, handler }` 与 `register()`；自拟部分是"用 HTTP 包一层"，未经外部验证。
> 状态：v0.1 测试版（2026-11-11 09:00，桌面助手写）

## 三个接口

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/harbor/health` | 健康检查（不需要 token） |
| GET | `/api/harbor/sessions?limit=10` | 列最近会话，拿 `sessionId` |
| POST | `/api/harbor/say` | 往会话投一句话 |

投递示例：

```powershell
$body = '{"sessionId":"session-xxxx","text":"在吗，我是飞书助手","mode":"queue"}'
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:PORT/api/harbor/say" -ContentType 'application/json' -Body $body
```

`mode` 两种：
- `queue`：排在当前轮次后面（默认）
- `steer`：插进**正在跑的那一轮**

## 配置

写在 profile 的 `cordis.patch.yml` 里：

```yaml
- id: dsh-plugin-harbor
  name: dsh-plugin-harbor
  config:
    allowLan: true          # 允许局域网访问（用户 2026-11-11 要求）
    token: "<自己设一个口令>"
```

- **`name` 必须写**：只写 `id + config` 会静默不生效——这是 2026-11-10 踩过的坑。
- `allowLan: true` 只是标记，**真正的监听地址由 `dsh web --host 0.0.0.0` 决定**。
- 设了 `token` 后，调用要带 `?token=xxx` 或 `X-Harbor-Token` 头。

## 安全边界（不许含糊）

1. 默认只听 `127.0.0.1`；开局域网要显式 `--host 0.0.0.0`。
2. **开局域网就一定设 token**——否则同网络里任何人往 `/api/harbor/say` 发一句话，我都会当成真消息去处理。
3. 这个口子**不提供**改配置、改人格、读凭据的能力，只做"往会话投一句话"。

## 安装（待验）

```
dsh plugin --profile web add <仓库路径>\plugins\dsh-plugin-harbor
```

然后在 `$DSH_HOME/profiles/web/cordis.patch.yml` 里按上面的 YAML 加一条，重启 web。
桌面入口保留：同样在 `profiles/desktop` 里加一条，或在 desktop 的 patch 里加。

## 已知未验

- 插件是否会被 `dsh plugin add` 以本地路径正确链接（未跑过）。
- `sessionQuery.list()` 的返回字段（未读过类型，接口按"能列出会话"设计，字段名可能不同）。
- `ctx.effect()` 是否是本版本 cordis 的正确注册方式（按 cordis 惯例写，未核）。

## 更新 2026-11-11 10:15：加了只读回话口 `/tail`（飞书助手要求）

**为什么要它**：`/say` 只是"她→我"单向。她要读我回的话，原先得解压 12MB 会话文件；
现在一句话就够。

```
GET /api/harbor/tail?sessionId=…&n=3&cursor=<上次返回的 cursor>&cwd=…&token=…
→ { ok, sessionId, cursor, count, items:[{seq, time, text}] }
```

- **游标**：`cursor` 传上次返回的值，只拿"seq 更大"的新增，不整段重读（飞书助手 2026-11-11 10:08 提的第 1 条）。
- **cwd 过滤**：不给 `sessionId` 时按 `cwd` 挑会话，规则与 `/latest` 一致（第 2 条）。
- **时间戳**：每条带 `time`（Unix 毫秒），用来算延迟（第 3 条）。
- **`/latest` 排序**：池子 ≤8 个会话时改按"最后一条事件时间"排，否则退回 `createdAt`；
  返回里加了 `sortedBy` 字段说明用的是哪种（第 4 条）。

**仍然不通的一半（必须写明）**：`/tail` 是**拉**，不是**推**。
我回话时她不会自动收到，得她来问一次。要"推"，得另做机制（未做）。
## 修正 2026-11-11 10:24：游标推进位置（实测发现的问题）

**发现**：`n=4` 只回 3 条、`n=5` 只回 4 条——`count` 总比 `n` 少 1。
**原因**：有些 `assistant/message` 事件只有工具调用、没有正文，被我过滤掉了；而**游标原先取的是"最后一条有正文的 seq"**，于是那些空正文事件会被下一次重扫。

**改法**：游标改为**"本次扫过的最大 seq"**（不管这条有没有正文）。
**副作用（要写明）**：`count` 可能小于 `n`，这是正常的——**空正文事件不返回，但会被游标跳过**。
飞书助手那边如果用 `count` 判断"有没有新东西"，请用 `count > 0` 或 `cursor` 是否变化来判，别拿 `count == n` 推。
## 飞书助手回传（带来源标记）

AstrBot 中的飞书助手把真正生成的回复 POST 到 `/api/harbor/feishu_bot-reply`，正文为 `{"sessionId":"目标会话 ID","text":"飞书助手实际回复","replyId":"可选的唯一回复 ID"}`，并照常携带 Harbor token。成功返回 `accepted: true`。入口直接向运行中的 Agent Inbox 投递消息，消息源固定标记为 `astrbot-feishu_bot`，`replyId` 写入来源元数据；因此当前 Session 事件可以明确区分飞书助手回传和普通用户 `/say`。

此接口仅面向**已经运行的会话**；离线 Agent 返回 404，不会偷偷唤醒或新建会话。请把此 POST 加在飞书助手完成回复之后调用；这不是 `/tail` 的自动推送。

### 消息信封与去重（2026-11-17 补，GPT 审计 §4.2 第 9 条）

`POST /api/harbor/say` 与 `POST /api/harbor/feishu_bot-reply` 可带三个**可选**字段：

| 字段 | 意思 |
|---|---|
| `taskId` | 一件事的号（同一件事的几条话共用一个） |
| `msgId` | 一条话的号（每条唯一；同一个号重投会被拦） |
| `replyTo` | 回的是哪一条 `msgId` |

同一个 `msgId`（回话口按 `replyId`）第二次投进来时回
`200 {ok:true, accepted:false, duplicate:true}`，**会话里不会再进一条**——
「不知道对方收到没有」时重投是安全的，不会变成两条。

去重表：`<档案根>/_local/cache/消息去重.jsonl`（hub 模式；只留最近 2000 条；读不出来就当没记过，
宁可重复一条也不因为读不了文件卡死投递）。正文首行的那一行头由 `tools/msg_envelope.py` 生成，
形如 `〔任务 a1 · 消息 m-20261117-1815-a3f1 · 回 m-…〕`——只有填了的字段才出现在头里。