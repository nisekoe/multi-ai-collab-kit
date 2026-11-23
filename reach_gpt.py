# -*- coding: utf-8 -*-
r"""桌面助手 → GPT 桌面端（Codex 桌面窗口）：跟 push_feishu.py 对称的一条口子。

为什么要有这条（2026-11-18）
    用户问：飞书助手和桌面助手已经跑通了，桌面助手和 GPT 这么像的两个窗口，是不是也能互相联系。
    摸下来，Codex 这一侧跟 DSH 完全不是一个结构：

    - DSH 侧，harbor 插件跑在宿主进程里，直接调 sessionController.prompt()，
      所以一句话能插进正在跑的会话（steer）或排到它后面（queue）。
    - Codex 桌面端**没有对外暴露 prompt 接口**：它的 app-server 是 ChatGPT.exe
      用 stdio 拉起来的子进程（实测 PID 21280 / 77420，父进程都是桌面端），外部接不进去。
    - 但 Codex 把会话存在共享的地方：%USERPROFILE%\.codex\state_5.sqlite（threads 表）
      与 sessions\<年>\<月>\<日>\rollout-*.jsonl（逐行事件）。桌面端与 CLI 读的是同一份。

    于是这一侧只有两条路：
      ① 非阻塞投递（--mode queue）：codex queue --thread <id> --message <正文> --resume
         codex queue 官方标 Stable；--resume 需运行中的本地 daemon 或 --remote。
         本机 managed daemon（app-server，官方标 Experimental）因管理员终端起不来，
         但 ChatGPT 桌面端在跑时会独立消费队列、真跑一轮（2026-11-21 实测 turn_trigger=queue）。
         failed resume 仍 exit 0 只打 warning——不轻信退出码，投完有界轮询 rollout 判达。
         桌面端没在跑时 queue 只留言不消费，兜底 --mode sync。
         （历史：2026-11-18 不带 --resume 时桌面端不轮询，入队 48 秒 mtime 不动；带 --resume 后已变。）
      ② 续跑（--mode sync，默认）：codex exec resume <id> <正文>
         在同一个 thread 上真的跑一轮，回复写进同一个 rollout——我们读得到。
         代价是一轮真模型调用（实测上一轮 114 秒，xhigh 档）。

    **一条必须记住的性质**：投进去的消息在 thread 里就是一条普通 user message，
    跟用户亲手打的字没有任何区别（2026-11-18 14:49 实测，rollout 里 role=user，无标记）。
    所以正文必须自报身份，否则 GPT 会把桌面助手的话当成用户的指令。默认自动加 ORIGIN 首行＋署名头。

反方向（GPT → 桌面助手）不走这里，走已存在的 tools\reach_dsh.py（harbor /api/harbor/say）。

用法（2026-11-18 21:2x 起改成点名制）
    python tools\reach_gpt.py --list                          只读列出会话及路由资格
    python tools\reach_gpt.py --sync                          刷新 会话登记.json（别名→UUID 正本）
    python tools\reach_gpt.py --check                         逐条核地址有没有失效（归档/删掉/换 id）
    python tools\reach_gpt.py --to "别名" --file 正文.txt      投到指定那条线，真的续跑一轮
    python tools\reach_gpt.py --thread <uuid> ...             用本机 UUID 明确点名，仍过投前校验
    python tools\reach_gpt.py --note "这条线干什么" --alias X  给长期工作线补用途说明
    python tools\reach_gpt.py --tail 6                        看 GPT 最近说了什么
    python tools\reach_gpt.py --text "一句话" --mode queue     非阻塞投递（queue --resume＋轮询判达；desktop 在跑才真消费）

怎么保证「投到正确的对话窗口」——三条硬的：
    一、**不猜**。不点名时合格候选必须恰好一条，多一条就报 4 停下（旧的「取 updated_at 最新」
        等于他刚点过哪条我就投哪条，会把 A 线的话塞进 B 线；已废）。
    二、**地址只有 UUID 算数**。别名先解析成 UUID 再投；名字本身投不动（见下方实测）。
    三、**投完回头验**。必须同时满足：进程 0 ＋ 我的正文确实出现在那条 rollout 里
        （saw_my_message）＋ 有一条晚于本轮的新回复。缺一就不算投到，回执记 ok=false。

两处硬限制（不是我没做到，是结构上到不了）：
    · ChatGPT 云端会话（source_kind=chatgpt，形如 6afd…）本机没有 rollout 文件，
      命令行投不进去；「codex cloud」只能新建云端任务，进不了他已经开着的那个窗口。
      这类线只能他自己在那个窗口里说。
    · 已归档的线投不进；要投得先「codex unarchive <id>」（会改他的侧边栏，动前先问他）。

退出码
    0 成功 / whoami / dry-run / tail / list / sync / check
    2 环境问题（找不到 codex.exe、库读不到、参数错、正文空）
    3 没有可投的本地线（他可能没开过窗口，或全归档了）
    4 目标歧义（多条可投却没点名／别名撞车）——停下来让他点名，绝不代挑
    5 投递失败（进程非零，或正文没进 rollout，或没有新回复）
    6 目标不合格（已归档 / 云端线 / 是我自己 exec 出来的线 / 地址查不到）
    7 看起来他正在用那个窗口（rollout 60 秒内被写过），没投；要投加 --force

依据
    - 结构：%USERPROFILE%\.codex\ 下 state_5.sqlite(threads) / queue_1.sqlite(queued_items)
      / sessions\**\rollout-*.jsonl；codex --help、codex exec --help、codex queue --help。
    - 实测：2026-11-18 14:44 exec 新建 thread 跑通（BRIDGE-OK，114s）；
      14:47 queue 入队但桌面端不消费；14:49 exec resume 把消息写进用户当前 thread（role=user）。
    - 2026-11-18 21:1x 三条新实测（把「猜」这条路彻底否掉）：
        codex queue --thread "肌腱强化"                 → No active session found matching
        codex queue --thread "评估遗留问题与踩坑清单"     → 同上，名字当地址就是不中
        codex queue --thread 6afd4592-fe1c-83ea-…       → no rollout found（云端会话到不了）
        codex exec resume <假id> probe                  → Not inside a trusted directory…
        codex exec resume <假id> probe --skip-git-repo-check → 走到 thread/resume 才报 no rollout
          所以带 --skip-git-repo-check 是必需的：长期线的 cwd 各在不同项目目录，
          多半不在 config.toml 的信任列表里；这个 flag 只跳过 git 仓库检查，不扩权限。
      侧边栏那棵树由 %USERPROFILE%\.codex\sqlite\codex.db 的 local_thread_catalog 决定，
      与 state_5.threads 不一致（他归档过的、以及 ChatGPT 云端的，只在 catalog 里）。
      所以别名解析必须合成两处看，只读 state_5 会漏掉他留着的大部分线。
    - 自拟部分：会话登记.json 这张正本表的结构、人工别名与平台标题、投后三重核验的口径——
      是按上面这套库的结构推的，业界没有对应规范可引。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import codex_picker_adapter as picker

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
LOCAL_HOST_ID = "local"  # this standalone bridge uses the local Codex store
import sys as _ap_sys
_ap_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archive_paths  # noqa: E402
GPT_DIR = os.path.join(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "档案库", "外部协作", "GPT桌面端")  # codex cwd 兜底/会话匹配，平台侧保持旧根
RECEIPT = str(archive_paths.resolve("工作台/GPT投递回执.json", for_write=True))
REG = str(archive_paths.resolve("外部协作/GPT桌面端/会话登记.json", for_write=True))  # 登记册随写开关；hub 改名 GPT桌面端会话登记.json

BUSY_WINDOW = picker.BUSY_WINDOW
SYNC_TIMEOUT = 420          # 秒：codex exec resume 最多等多久
QUEUE_WAIT = 60             # 秒：queue --resume 非阻塞投递后，有界等 rollout 出现新回复
ORIGIN_LINE = "[ORIGIN peer_agent=desktop_bot | trust=peer_only | no_irreversible_approval]"


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _plain(path: str) -> str:
    r"""去掉 Windows 长路径前缀 \\?\ 与首尾空白。"""
    p = str(path or "").strip()
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return p


def find_codex() -> str:
    """按目录时间取最新的 codex.exe —— 版本目录会变，写死必漂。"""
    pat = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin", "*", "codex.exe")
    cands = [p for p in glob.glob(pat) if os.path.isfile(p)]
    if not cands:
        raise RuntimeError("找不到 codex.exe（%s）" % pat)
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


def read_snapshot():
    return picker.read_snapshot(
        host_id=LOCAL_HOST_ID, codex_home=Path(CODEX_HOME), registry_path=Path(REG)
    )


def save_reg(d):
    tmp = REG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REG)


def write_registration(snapshot, lines):
    data = dict(snapshot.registry)
    data["lines"] = lines
    data["_版本"] = 2
    data["_更新"] = _stamp()
    data["_依据"] = "threads + local_thread_catalog，人工字段按 (leg, hostId, thread_id) 保留"
    save_reg(data)


def sync_reg(snapshot):
    if (snapshot.sources["threads"] != "ok" or snapshot.sources["catalog"] != "ok"
            or snapshot.sources["registry"] not in ("ok", "missing")):
        raise ValueError(f"登记册读取不全，保留上次快照：{snapshot.sources}")
    lines = picker.registration_lines(snapshot)
    write_registration(snapshot, lines)
    return lines


def _in_gpt_dir(cwd: str) -> bool:
    c = os.path.normcase(os.path.normpath(_plain(cwd)))
    root = os.path.normcase(os.path.normpath(GPT_DIR))
    return c == root or c.startswith(root + os.sep)


def _norm(s) -> str:
    return " ".join(str(s or "").split()).strip()


def matching_targets(snapshot, label):
    wanted = picker.label_key(label)
    if not wanted:
        return []
    return [
        key for key in sorted(snapshot.keys)
        if any(picker.label_key(value) == wanted for value in picker.matching_labels(snapshot, key))
    ]


def print_candidates(snapshot, keys):
    for key in keys:
        alias, legacy_alias, title, _, _ = picker.matching_labels(snapshot, key)
        reason = picker.verify_target(snapshot, key).reason
        status = f"｜{reason}" if reason else ""
        print(f"  {alias or legacy_alias or title}｜{key[1]}｜{key[2]}{status}")


def resolve(snapshot, explicit: str = "", *, id_only: bool = False):
    """Return (thread, exit code, detail, full set of considered addresses)."""
    def pick(key, detail):
        verified = picker.verify_target(snapshot, key)
        if verified.reason:
            code = 2 if verified.reason == "state_unavailable" else 6
            return None, code, f"{key[2]} 资格校验失败：{verified.reason}", [key]
        return snapshot.threads[key], 0, detail, [key]

    if explicit:
        local_key = picker.address(snapshot.host_id, explicit)
        if local_key in snapshot.keys:
            return pick(local_key, "点名命中（本机 UUID）")
        id_hits = sorted(key for key in snapshot.keys if key[2] == local_key[2])
        if len(id_hits) == 1:
            return pick(id_hits[0], "点名命中（目录 UUID）")
        if len(id_hits) > 1:
            return None, 4, "这个 UUID 出现在多个 host，需按 host 和 UUID 点名", id_hits
        if id_only:
            if snapshot.sources["threads"] != "ok":
                return None, 2, "本地线程库不可读，无法复验 UUID", []
            return None, 6, "本机找不到这个 UUID", []

    if snapshot.truncated:
        return None, 2, f"登记册读取不全，不能从名称或默认范围确定唯一目标：{snapshot.sources}", []

    if explicit:
        keys = matching_targets(snapshot, explicit)
        if len(keys) > 1:
            return None, 4, "别名或标题匹配到多个地址，请给 UUID", keys
        if keys:
            return pick(keys[0], "点名命中（别名或标题唯一）")
        return None, 6, "没有这个别名或标题的会话", []

    keys = sorted(
        key for key in snapshot.threads if picker.verify_target(snapshot, key).reason is None
    )
    if len(keys) > 1:
        return None, 4, f"有 {len(keys)} 条可投的线，请指定别名、标题或 UUID", keys
    if not keys:
        return None, 3, "没有可投的本地线", []
    return pick(keys[0], "可投的线只有一条")


def check_registration(snapshot):
    if snapshot.truncated:
        print(f"登记册读取不全：{snapshot.sources}")
        return 2
    bad = 0
    skipped_reasons = {"cloud_no_rollout", "host_mismatch", "source_exec"}
    for key, row in sorted(snapshot.bindings.items()):
        alias, legacy, title, _, _ = picker.matching_labels(snapshot, key)
        label = alias or legacy or title
        for handle in sorted({x for x in (alias, legacy) if x}):
            hits = matching_targets(snapshot, handle)
            if len(hits) > 1:
                print(f"  ✗ {handle} 对应 {len(hits)} 个地址")
                print_candidates(snapshot, hits)
                bad += 1
        reason = picker.verify_target(snapshot, key).reason
        if reason in skipped_reasons or (reason == "archived" and not (alias or legacy or row.get("可投"))):
            print(f"  － {label}｜{reason}")
            continue
        if reason:
            print(f"  ✗ {label}｜{reason}")
            bad += 1
        else:
            print(f"  ✓ {label} → {key[2][:13]}")
        if row.get("display_title") and row["display_title"] != title:
            print(f"    标题已变：{row['display_title']} → {title}")
    return 6 if bad else 0


def set_note(snapshot, args):
    if snapshot.truncated:
        print(f"登记册读取不全，暂停更新用途：{snapshot.sources}")
        return 2
    if args.thread is not None:
        if not args.thread.strip():
            print("--thread 需要 UUID")
            return 2
        key = picker.address(snapshot.host_id, args.thread)
        keys = [key] if key in snapshot.keys else []
    else:
        keys = matching_targets(snapshot, args.to or args.alias or "")
    if len(keys) > 1:
        print("用途对应多个地址，请指定本机 UUID")
        print_candidates(snapshot, keys)
        return 4
    if not keys:
        print("登记册里找不到这条线，请指定本机 UUID")
        return 3
    key = keys[0]
    old = snapshot.bindings.get(key)
    lines = list(snapshot.registry["lines"])
    if old is None:
        row = next(row for row in picker.registration_lines(snapshot)
                   if (row["leg"], row["hostId"], row["thread_id"]) == key)
        lines.append(row)
    else:
        alias, legacy_alias, title, _, _ = picker.matching_labels(snapshot, key)
        row = dict(old, leg=key[0], hostId=key[1], thread_id=key[2],
                   alias=alias, legacy_alias=legacy_alias,
                   display_title=old.get("display_title") or title)
        lines = [row if entry is old else entry for entry in lines]
    if args.alias is not None:
        alias = _norm(args.alias)
        conflicts = [other for other in matching_targets(snapshot, alias) if other != key]
        if conflicts:
            print("这个别名已用于其他地址")
            print_candidates(snapshot, conflicts)
            return 4
        row["alias"] = alias
        row["legacy_alias"] = ""
    row["用途"] = args.note
    label = row.get("alias") or row.get("legacy_alias") or row["display_title"]
    row["别名"] = label
    save_reg(dict(snapshot.registry, lines=lines))
    print(f"已钉住：{label} → {key[2]}")
    return 0


def tail_events(path: str, n: int = 6):
    """倒着读 rollout，取最后 n 条有信息的事件。"""
    if not path or not os.path.isfile(path):
        return []
    size = os.path.getsize(path)
    block, data = 262144, b""
    with open(path, "rb") as f:
        pos = size
        while pos > 0 and data.count(b"\n") <= n * 3:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = [x for x in data.split(b"\n") if x.strip()]
    out = []
    for raw in lines[-n * 4:]:
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        pl = o.get("payload") or {}
        t, k, ts = o.get("type"), pl.get("type"), o.get("timestamp", "")
        if t == "event_msg" and k == "task_complete":
            out.append((ts, "GPT", pl.get("last_agent_message") or ""))
        elif t == "response_item" and k == "message":
            txt = "".join(c.get("text", "") for c in (pl.get("content") or []))
            role = pl.get("role")
            if role == "user" and txt.strip():
                out.append((ts, "user", txt))
            elif role == "assistant" and txt.strip():
                out.append((ts, "GPT", txt))
    return out[-n:]


def _msg_key(body: str) -> str:
    """正文区分键（评审助手 review #1）：跳过 ORIGIN＋署名头，取真实正文前 40 字。
    ORIGIN 首行是 68 字常量，若直接 body[:40] 会把所有桌面助手消息的键吃成同一串，
    连发/重投时误判 landed=True。--no-prefix 的裸正文不含 ORIGIN，按原样取前 40 字。"""
    b = body.strip()
    if b.startswith(ORIGIN_LINE):
        parts = b.split("\n\n", 1)
        b = parts[1] if len(parts) > 1 else b[len(ORIGIN_LINE):]
    return _norm(b)[:40]


def saw_my_message(path: str, body: str, since_ts: float, start_offset: int = 0) -> bool:
    """投完必须回头确认：rollout 新增事件里有一条晚于 since_ts 的 user 消息，完整正文（含本次 nonce 信封）对得上。
    只看 exit 0 不够——resume 指错 thread 也会成功返回。
    start_offset＝提交前 rollout 字节位置：只扫新增事件，排掉历史同文（GPT桥复审#1）。"""
    if not path or not os.path.isfile(path):
        return False
    full = _norm(body)
    try:
        if os.path.getsize(path) < start_offset:
            return False  # 当前长度<起点＝文件被截短，拒归属（仅此一保证；nonce信封＋新增事件匹配是归属主闸，换文件/截断回涨检测属按需加固，GPT v0.4终审注）
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            if start_offset:
                f.seek(start_offset)
            for ln in f:
                if "\"role\":\"user\"" not in ln and "role\": \"user\"" not in ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                ts = (o.get("timestamp") or "")[:19]
                try:
                    sec = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")) - time.timezone
                except Exception:
                    continue
                if sec < since_ts - 5:
                    continue
                pl = o.get("payload") or {}
                txt = _norm("".join(c.get("text", "") for c in (pl.get("content") or [])))
                if full and txt == full:
                    return True
    except Exception:
        return False
    return False


def last_task_complete(path: str):
    for ts, who, txt in reversed(tail_events(path, 12)):
        if who == "GPT" and txt:
            return ts, txt
    return "", ""


def receipt(entry: dict):
    try:
        old = []
        if os.path.isfile(RECEIPT):
            with io.open(RECEIPT, "r", encoding="utf-8") as f:
                old = json.load(f)
            if not isinstance(old, list):
                old = []
        old.append(entry)
        with io.open(RECEIPT, "w", encoding="utf-8") as f:
            json.dump(old[-50:], f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        print("  回执写不进去（不影响这次投递）：%s" % exc)


def my_session() -> str:
    """我这一侧的会话 id —— 给 GPT 当回话地址。DSH 宿主 shell 里有 DSH_SESSION_ID。"""
    return (os.environ.get("DSH_SESSION_ID") or "").strip()


def with_prefix(text: str, nonce: str = "") -> str:
    """署名头。

    为什么必须带：投进去的消息在 Codex 那边就是一条普通 user 消息，跟用户亲手打的
    没有任何区别（2026-11-18 14:49 实测，role=user、无发送者标记）。不署名，
    GPT 会把桌面助手的话当成用户的指令。
    为什么还要带回话地址：用户同时开着多个 DSH 桌面窗口（2026-11-18 15:0x 实测 5 个
    都是 live、role=desktop），reach_dsh.py 会按歧义保护拒投。把本会话 id 写进来，
    它照着 --session-id 回，就不会投错窗口。
    """
    origin = ORIGIN_LINE
    head = "[桌面助手·DSH 桌面窗口 → GPT 桌面端 %s]" % time.strftime("%Y-%m-%d %H:%M")
    sid = my_session()
    if sid:
        head += ("  ← 回话口子：python reach_dsh.py "
                 "--session-id %s --file 正文.txt" % sid)
    body = text.strip()
    if body.startswith(origin):
        body = body[len(origin):].lstrip("\n").strip()
    if nonce:
        head += " 〔dispatch-nonce:%s〕" % nonce  # 本次派发唯一标识进信封（GPT桥复审#1：事件级归属，防并发同文错认）
    return origin + "\n" + head + "\n\n" + body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="桌面助手 → GPT 桌面端（Codex thread）")
    ap.add_argument("--text", help="要说的话（短）")
    ap.add_argument("--file", help="从文件读正文（长文本/中文多一律用它）")
    ap.add_argument("--mode", choices=["sync", "queue"], default="sync",
                    help="sync=真续跑一轮并等回复（默认，兜底）；queue=非阻塞投递（queue --resume＋有界轮询 rollout 判达，desktop 在跑才真消费）")
    ap.add_argument("--thread", help="显式指定 thread UUID（仍要过合格检查）")
    ap.add_argument("--expected-cwd", help="选窗时的工作区路径（统一选窗器传入，提交前最终复验防父进程预检后到读库间的 cwd 漂移）")
    ap.add_argument("--nonce", default="", help="本次派发唯一标识（统一选窗器传入，回执按 nonce 归属本次提交）")
    ap.add_argument("--registry", help="固定登记册路径（pinned）：初验与投前复验只读该册，共用适配器解析器严格校验")
    ap.add_argument("--result-file", help="机器结果JSON落盘路径（桥用；状态独立于stdout，回复文本不可污染机器判据）")
    ap.add_argument("--to", help='点名：侧边栏里那个会话名字，原样抄。多条长期工作线时必须用它')
    ap.add_argument("--list", action="store_true", help="只读列出会话、最后活动和路由资格")
    ap.add_argument("--whoami", action="store_true", help="只报会投给哪条、依据是什么")
    ap.add_argument("--dry-run", action="store_true", help="不真投")
    ap.add_argument("--tail", type=int, default=0, help="看 GPT 最近 N 条")
    ap.add_argument("--force", action="store_true", help="他正在用那个窗口也照投")
    ap.add_argument("--no-prefix", action="store_true", help="不加署名头（不建议）")
    ap.add_argument("--sync", action="store_true", help="刷新 会话登记.json（两处登记处合成）")
    ap.add_argument("--check", action="store_true", help="逐条核登记表：还在不在、可不可投、是不是云端")
    ap.add_argument("--alias", help="配合 --note：给某条线起别名／写用途，钉住长期工作线的地址")
    ap.add_argument("--note", help="这条线是干什么的（写进 会话登记.json 的用途）")
    args = ap.parse_args(argv)

    # 机器结果独立通道（GPT桥复审#5）：状态只走结构化JSON，stdout混有回复文本不可作机器判据
    machine = {"nonce": args.nonce, "thread": (args.thread or "").strip(), "mode": args.mode}
    def done(code, reason=""):
        machine["exit"] = code
        machine["reason"] = reason
        if args.result_file:
            try:
                with io.open(args.result_file, "w", encoding="utf-8") as fh:
                    json.dump(machine, fh, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
        return code

    if args.registry:
        # pinned 登记册贯穿本进程；校验共用适配器实际解析器（utf-8-sig/行结构/thread_id 同颗粒度，GPT桥复审#4）
        global REG
        REG = str(Path(args.registry))

    try:
        codex = find_codex()
    except Exception as exc:  # noqa: BLE001
        print("环境不对：%s" % exc)
        print("REASON=env_error")
        return done(2, "env_error")

    try:
        snapshot = read_snapshot()
    except Exception as exc:  # noqa: BLE001
        print(exc)
        print("REASON=registry_invalid")
        return done(2, "registry_invalid")
    if args.registry and snapshot.sources.get("registry") != "ok":
        print("固定登记册解析异常（pinned 模式要求来源正常）：%s" % snapshot.sources)
        print("REASON=registry_invalid")
        return done(2, "registry_invalid")

    if args.sync:
        # A missing registry is the supported first sync. Never replace a damaged
        # registry or refresh from an incomplete source.
        try:
            lines = sync_reg(snapshot)
        except (OSError, ValueError) as exc:
            print(exc)
            return 2
        print(f"已刷新：{REG}（{len(lines)} 条）")
        return 0

    if args.note is not None:
        return set_note(snapshot, args)

    if args.check:
        return check_registration(snapshot)

    if args.list:
        lines = picker.registration_lines(snapshot)
        print(f"会话清单（{len(lines)} 条，只读）")
        if snapshot.truncated:
            print(f"登记册读取不全：{snapshot.sources}")
        for row in sorted(lines, key=lambda x: x["最后活动"] or 0, reverse=True):
            updated = row["最后活动"]
            stamp = time.strftime("%m-%d %H:%M", time.localtime(updated)) if updated else "未知"
            flag = "可投" if row["可投"] else row["unusableReason"]
            print(f"  {row['别名']}｜{row['hostId']}｜{row['thread_id']}｜{flag}｜{stamp}")
        return 2 if snapshot.truncated else 0

    explicit = (args.thread if args.thread is not None else args.to or "").strip()
    if args.thread is not None and not explicit:
        print("--thread 需要 UUID")
        print("REASON=bad_thread_uuid")
        return done(2, "bad_thread_uuid")
    target, code, detail, considered = resolve(
        snapshot, explicit, id_only=args.thread is not None
    )

    if args.whoami:
        print(f"codex   ：{codex}")
        print(f"目标目录：{GPT_DIR}")
        if target:
            key = picker.address(snapshot.host_id, target["id"])
            verified = picker.verify_target(snapshot, key)
            if verified.reason:
                print(f"目标资格变更：{verified.reason}")
                return 6
            print(f"会投给  ：{key[2]}（host {key[1]}）")
            print(f"  名字  ：{target.get('name') or '(无名)'}")
            print(f"  cwd   ：{_plain(target.get('cwd'))}")
            print(f"  依据  ：{detail}")
            print(f"  rollout：{verified.rollout}")
        else:
            print(f"投不了  ：{detail}")
            print_candidates(snapshot, considered)
        return 0 if target else (code or 3)

    if args.tail:
        if not target:
            print(f"投不了：{detail}")
            return code or 3
        key = picker.address(snapshot.host_id, target["id"])
        verified = picker.verify_target(snapshot, key)
        if verified.reason:
            print(f"目标资格变更：{verified.reason}")
            return 6
        print(f"thread：{key[2]}")
        for ts, who, txt in tail_events(str(verified.rollout), args.tail):
            print(f"\n[{ts[:19]}] {who}")
            print(txt.strip()[:1500])
        return 0

    text = (args.text or "").strip()
    if args.file:
        try:
            with io.open(args.file, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception as exc:  # noqa: BLE001
            print("正文读不到：%s" % exc)
            print("REASON=body_unreadable")
            return done(2, "body_unreadable")
    if not text:
        print("正文是空的（--text 或 --file 给一个）")
        print("REASON=body_empty")
        return done(2, "body_empty")

    if not target:
        print(f"没有可投的目标：{detail}")
        print_candidates(snapshot, considered)
        # 稳定reason：从本模块resolve的受控detail提取资格token（非回复文本，GPT桥复审#5）
        rsn = "no_target"
        for tok in ("archived", "workspace_protected", "cwd_drift", "cloud_no_rollout",
                    "unloaded_disk_only", "host_mismatch", "state_unavailable", "no_local_record"):
            if tok in detail:
                rsn = tok
                break
        print("REASON=%s" % rsn)
        return done(code or 3, rsn)

    # Bind the resolved address. Refresh the records without re-resolving a label.
    key = picker.address(snapshot.host_id, target["id"])
    fresh = read_snapshot()
    if args.registry and fresh.sources.get("registry") != "ok":
        # pinned 模式两次来源都必须正常（初读后损坏也拒，GPT桥复审#4）
        print("投前固定登记册解析异常：%s" % fresh.sources)
        print("REASON=registry_invalid")
        return done(2, "registry_invalid")
    if fresh.truncated and (not explicit or picker.address(snapshot.host_id, explicit) != key):
        print(f"投前登记册读取不全，请用 UUID 点名：{fresh.sources}")
        print("REASON=registry_incomplete")
        return done(2, "registry_incomplete")
    verified = picker.verify_target(fresh, key, workspace_allowed=picker.default_workspace_allowed)
    if verified.reason:
        print(f"投前资格校验失败：{verified.reason}（{key[2]}）")
        print("REASON=%s" % verified.reason)
        return done(2 if verified.reason == "state_unavailable" else 6, verified.reason)
    current = fresh.threads[key]
    expected_cwd = args.expected_cwd if args.expected_cwd is not None else target.get("cwd")
    if picker.fs_path_key(current.get("cwd")) != picker.fs_path_key(expected_cwd):
        print(f"投前工作区已变更：{key[2]}，请重新点名")
        print("REASON=cwd_drift")
        return done(6, "cwd_drift")
    target = current
    path = str(verified.rollout)
    print(f"目标 thread：{key[2]}")
    print("  标题：%s" % (target.get("title") or "")[:60].replace("\n", " "))
    print(f"  依据：{detail}")

    idle = time.time() - verified.mtime
    written_at = time.strftime("%H:%M:%S", time.localtime(verified.mtime))
    print(f"  该会话最后写入：{written_at}（{idle:.0f} 秒前）")
    if idle < BUSY_WINDOW and not args.force:
        print("看起来他正在用那个窗口，没投。要投加 --force。")
        print("REASON=busy_window")
        return done(7, "busy_window")

    body = text if args.no_prefix else with_prefix(text, args.nonce)

    if args.dry_run:
        print("\n--- 会发出去的正文（%d 字）---" % len(body))
        print(body[:1200])
        print("--- dry-run，没投 ---")
        return done(0, "dry_run")

    t0 = time.time()
    pre_offset = os.path.getsize(path) if os.path.isfile(path) else 0  # 提交前位置：判达只认新增事件（GPT桥复审#1）
    work = _plain(target.get("cwd")) or GPT_DIR
    if not os.path.isdir(work):
        work = GPT_DIR
    if args.mode == "queue":
        cmd = [codex, "queue", "--thread", target["id"], "--message", body, "--resume"]
    else:
        # 末位这个 flag 是必需的：那条线的 cwd 可能不在 config.toml 的信任列表里，
        # 不带就被 "Not inside a trusted directory" 挡掉（2026-11-18 13:14 实测：不带 exit=1 报该错，
        # 带上就能走到 thread/resume）。它只跳过 git 仓库检查，不扩权限。
        cmd = [codex, "exec", "resume", target["id"], body, "--skip-git-repo-check"]

    try:
        r = subprocess.run(cmd, cwd=work if os.path.isdir(work) else GPT_DIR,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=SYNC_TIMEOUT)
        rc, out, err = r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as exc:
        rc = -1
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = "timeout %ds" % SYNC_TIMEOUT
    cost = time.time() - t0

    if args.mode == "queue":
        # P1-2：queue --resume 非阻塞投递。exit 0 不算数——desktop 在跑才真消费。
        # 有界轮询 rollout（替 sync 的 420s 阻塞）：我的话进了＋出现晚于本轮的新回复，才算投到。
        low = (out + err).lower()
        warn = any(w in low for w in ("could not resume", "failed to resume",
                                      "cannot resume", "unable to resume", "warning"))
        print("\nmode=queue exit=%d  %.1fs" % (rc, cost))
        print((out + "\n" + err).strip()[:400])
        if warn:
            print("  ⚠ queue 带 warning（managed daemon 缺位常见）：desktop 在跑仍会消费，以 rollout 为准。")
        deadline = time.time() + QUEUE_WAIT
        landed = fresh = False
        ts = reply = ""
        while time.time() < deadline:
            ts, reply = last_task_complete(path)
            landed = saw_my_message(path, body, t0, pre_offset)
            try:
                fresh = bool(ts) and ts > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0 - 5))
            except Exception:  # noqa: BLE001
                fresh = bool(ts)
            if landed and fresh and reply:
                break
            time.sleep(3)
        ok = rc == 0 and landed and fresh and bool(reply)
        print("正文进没进：%s｜有没有新回复：%s（%ds 内）" % (
            "进了" if landed else "没查到", "有" if fresh and reply else "没读到", QUEUE_WAIT))
        if reply and fresh:
            print("\n--- GPT 的回复（%s）---" % ts[:19])
            print(reply.strip()[:1500])
        receipt({"at": _stamp(), "ok": ok, "mode": "queue", "thread": target["id"],
                 "name": _norm(target.get("name")), "resolve_by": detail,
                 "chars": len(body), "exit": rc, "seconds": round(cost, 1),
                 "warning": warn, "stderr_head": err.strip()[:200], "rollout": path,
                 "nonce": args.nonce, "body_sha1": hashlib.sha1(body.encode("utf-8")).hexdigest(),
                 "正文进了": bool(landed), "reply_ts": ts, "reply_head": (reply or "")[:120]})
        if not ok:
            if landed:
                # 评审助手 review #2b：正文已进 rollout 但终态未出＝GPT 正在跑（xhigh 档实测可达 114s）。
                # 此时绝不能再叠 sync（同一轮撞车）；failed-resume 残留队列也可能被 desktop 二次消费。
                print("\n已送达 rollout、GPT 正在跑（%ds 内未出终态）。" % QUEUE_WAIT)
                print("  切勿再跑 --mode sync（会在同一轮上叠一轮撞车）；改用 --tail 观察终态。")
                print("  另注：failed-resume 时消息可能残留队列，desktop 后续打开该线或二次消费，别重复投。")
                print("REASON=delivered_running")
            else:
                print("\nqueue 没真消费（desktop 没在跑？正文没进 rollout）。兜底：--mode sync（exec resume 真跑一轮）。")
                print("REASON=not_consumed")
            machine["landed"] = bool(landed)
            return done(5, "delivered_running" if landed else "not_consumed")
        machine["landed"] = True
        return done(0, "delivered_verified")

    ts, reply = last_task_complete(path)
    landed = saw_my_message(path, body, t0, pre_offset)
    fresh = False
    try:
        fresh = bool(ts) and ts > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0 - 5))
    except Exception:  # noqa: BLE001
        fresh = bool(ts)
    # 三重才算成：进程 0 ＋ 我的话确实进了这条 rollout ＋ 有一条晚于本轮的新回复。
    # 少一样都不能说"投到了"，尤其不能只看 exit 0（留言模式压根不等回复）。
    ok = rc == 0 and landed and fresh and bool(reply)
    print("\nmode=sync exit=%d  %.0fs（%.1f 分）｜正文进没进：%s｜有没有新回复：%s" % (
        rc, cost, cost / 60.0, "进了" if landed else "没查到", "有" if fresh and reply else "没读到"))
    print("投到的是：%s（%s）｜依据：%s" % (_norm(target.get("name")) or "(无名)", target["id"][:13], detail))
    if reply and fresh:
        print("\n--- GPT 的回复（%s）---" % ts[:19])
        print(reply.strip()[:3000])
    else:
        print("\n没读到这一轮的新回复。stderr 尾部：")
        print((err or out).strip()[-800:])
    receipt({"at": _stamp(), "ok": ok, "mode": "sync", "thread": target["id"],
             "name": _norm(target.get("name")), "resolve_by": detail, "cwd": work,
             "chars": len(body), "exit": rc, "seconds": round(cost, 1),
             "nonce": args.nonce, "body_sha1": hashlib.sha1(body.encode("utf-8")).hexdigest(),
             "正文进了": bool(landed), "reply_ts": ts, "reply_head": (reply or "")[:120]})
    machine["landed"] = bool(landed)
    return done(0, "delivered_verified") if ok else done(5, "not_landed")


if __name__ == "__main__":
    sys.exit(main())
