#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""comm_gate.py —— 协作节奏闸（内部守则（略） §六 的道具）

它回答三个问题，一个都不许缺：
  1. 门口堆了几条？（未读条数）
  2. 堆的是什么？（意见 / 牢骚 / 继续 / 求助 各几条）
  3. 我现在该干什么？（BATCH / SILENT / PROCEED / WAIT / IDLE）

为什么必须有它：
  **"门口堆了几条"这件事，人眼看不见、模型也看不见** —— 模型只看到"被投进来的那一条"。
  看不见队列，就没有背压；没有背压，就是 2026-11-11 早上那场事故（她投 5~6 条，我还在做第 1 条）。

用法（必须用 AstrBot 那个解释器，它带 zstandard）：
  python comm_gate.py
  python comm_gate.py --json              # 机器可读
  python comm_gate.py --session <path>    # 指定会话文件
  python comm_gate.py --sessions          # 只列会话，不判定

退出码：
  0 = 判定出来了（不管判定是什么）
  2 = 环境问题（找不到会话 / 没 zstandard）

判定规则（照 内部守则（略） §四，不另立一套）：
  有【求助】                      → PROCEED
  ≥2 条且有【意见】                → BATCH
  ≥2 条且全是【牢骚】              → SILENT
  最后一条是【继续】且队列空        → PROCEED
  队列 ≥2 且我还没开工（刚醒）      → WAIT
  队列空                          → IDLE

来源怎么标：
  港湾投进来的和用户本人打的，source.kind 都是 `user`（这是 内部守则（略） §九 第 1 条未解的问题）。
  所以本工具**只能**报"来源不明(user)"，不许猜成"飞书助手"或"用户"。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SESSIONS_ROOT = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
CWD_KEY = os.environ.get("DSH_CWD_KEY") or ("--" + os.getcwd().replace(":", "").replace(chr(92), "-").replace("/", "-") + "--")

# 四类消息的标记（内部守则（略） §三：发的人写标记；没写就由我们兜底判，判不准当牢骚）
MARK_OPINION = ("【意见】", "[意见]")
MARK_NOISE = ("【牢骚】", "[牢骚]")
MARK_GO = ("【继续】", "[继续]")
MARK_HELP = ("【求助】", "[求助]")

# 兜底判据（没有标记时）：这些词出现就倾向某类
HEUR_HELP = ("帮我查", "你帮我", "你查一下", "需要你", "请你帮", "我确定不了")
HEUR_GO = ("继续吧", "接着继续", "我找到了", "可以了", "往下做",
           "开干", "开始做", "继续做", "接着做")
# 2026-11-11 22:2x 实测补：用户只说一个字的推进令也很常见（当时"动"被判成【牢骚】，判反了）。
# **单字必须用"整句精确匹配"**——拿单字去做子串匹配会误伤（"今天好累啊"会被"好"判成继续）。
EXACT_GO = {"动", "做", "上", "好", "行", "继续", "开始", "开干", "go", "ok",
            "可以", "动吧", "做吧", "来吧"}
HEUR_OPINION = ("请改", "要改", "改成", "不对", "矛盾", "错了", "应改为", "建议改")

WAIT_SECONDS = 30  # 刚要开工时，先等一会儿看还有没有补的


def find_sessions() -> list[str]:
    out = []
    base = os.path.join(SESSIONS_ROOT, CWD_KEY)
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        p = os.path.join(base, name, "session.v4.jsonl.zstd")
        if os.path.exists(p):
            out.append(p)
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def read_events(path: str) -> list[dict]:
    try:
        import zstandard  # noqa: PLC0415 —— 只有 AstrBot 的解释器装了
    except Exception:  # noqa: BLE001
        print("这个解释器没有 zstandard。请用 AstrBot 的解释器：")
        print(r"  项目根\uv\tools\astrbot\Scripts\python.exe")
        raise SystemExit(2)
    raw = open(path, "rb").read()
    data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def classify(text: str) -> str:
    """归到四类之一。有标记看标记；没标记按词兜底；都不中就当牢骚（代价最小）。"""
    t = text or ""
    if any(m in t[:40] for m in MARK_HELP):
        return "求助"
    if any(m in t[:40] for m in MARK_GO):
        return "继续"
    if any(m in t[:40] for m in MARK_OPINION):
        return "意见"
    if any(m in t[:40] for m in MARK_NOISE):
        return "牢骚"
    # 无标记兜底
    if any(k in t for k in HEUR_HELP):
        return "求助"
    if any(k in t for k in HEUR_GO) and len(t) < 120:
        return "继续"
    # 极短推进令：**整句精确匹配**（单字不能拿去做子串）
    if t.strip().rstrip("。！!").lower() in EXACT_GO:
        return "继续"
    if any(k in t for k in HEUR_OPINION):
        return "意见"
    return "牢骚"


def collect(events: list[dict]) -> tuple[list[dict], dict | None]:
    """返回 (所有投进来的消息, 我最后一轮的信息)。"""
    msgs = []
    last_turn_end = None
    for ev in events:
        typ = ev.get("type") or ""
        seq = ev.get("seq")
        ms = ev.get("time") or 0
        d = ev.get("data") or {}
        if typ == "turn/end":
            last_turn_end = {"seq": seq, "time": ms}
        if typ != "agent/inbox/spliced":
            continue
        for it in (d.get("inserted") or []):
            src = it.get("source") or {}
            txt = "".join(c.get("text", "") for c in (it.get("content") or [])
                          if isinstance(c, dict) and c.get("type") == "text")
            txt = txt.strip()
            if not txt:
                continue
            # 来源识别（内部守则（略） §十 10.1 第1条）：她发了「【飞书助手】」前缀就能认出来。
            # 没前缀的都算"不明" —— 因为港湾投的和用户打的 kind 都是 user，猜不得。
            body_text = txt.lstrip()
            if body_text.startswith("【飞书助手】"):
                sender = "飞书助手"
                body_text = body_text[len("【飞书助手】"):].lstrip()
            elif body_text.startswith("【用户】"):
                sender = "用户"
                body_text = body_text[len("【用户】"):].lstrip()
            else:
                sender = "不明"
            msgs.append({
                "seq": seq,
                "time": ms,
                "kind": src.get("kind") or "?",
                "sender": sender,
                "target": d.get("target"),
                "text": txt,
                "cls": classify(body_text),
            })
    return msgs, last_turn_end


def hhmmss(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000)) if ms else "?"


def report_rate(msgs: list[dict], now_ms: int) -> int:
    """内部守则（略） §十 10.2：数最近一小时各来源几条、最短间隔多少 —— 她自己也能跑。

    判据照 内部守则（略） §五：同一条线间隔 <10 分钟、或一小时内 ≥3 条 → 太快。
    """
    print("=" * 58)
    print("最近一小时的消息频率（内部守则（略） §五：间隔 ≥10 分钟、每小时 ≤3 条）")
    print("=" * 58)
    hour = [m for m in msgs if (now_ms - (m.get("time") or 0)) < 3600_000]
    if not msgs:
        print("一条都没有。")
        return 0

    by = {}
    for m in hour:
        by[m.get("sender", "不明")] = by.get(m.get("sender", "不明"), 0) + 1
    print("近一小时共 %d 条：%s" % (len(hour),
          " / ".join("%s %d" % (k, v) for k, v in sorted(by.items())) or "无"))

    ts = sorted((m.get("time") or 0) for m in msgs if m.get("time"))
    gaps = [(b - a) / 1000.0 for a, b in zip(ts, ts[1:])]
    if gaps:
        print("最短间隔：%.0f 秒（共 %d 个间隔）" % (min(gaps), len(gaps)))
        print("最近一条距今：%.0f 秒" % ((now_ms - ts[-1]) / 1000.0))

    print("-" * 58)
    for who in ("飞书助手", "不明"):
        if who not in by:
            continue
        n = by[who]
        mine = [m for m in hour if m.get("sender") == who]
        mts = sorted((m.get("time") or 0) for m in mine)
        mg = [(b - a) / 1000.0 for a, b in zip(mts, mts[1:])]
        shortest = min(mg) if mg else None
        too_many = n >= 3
        too_fast = shortest is not None and shortest < 600
        verdict = "太快" if (too_many or too_fast) else "正常"
        print("%s：近一小时 %d 条%s → %s"
              % (who, n,
                 ("，最短间隔 %.0f 秒" % shortest) if shortest is not None else "",
                 verdict))
        if who == "不明":
            print("   （没标【飞书助手】/【用户】前缀的，只能算进「不明」，数不准 —— "
                  "内部守则（略） §十 10.1 第1条就是为这个）")
    print("=" * 58)
    return 0


def decide(unread: list[dict], last_end: dict | None, now_ms: int) -> tuple[str, str]:
    """按 内部守则（略） §四 判。返回 (判定, 理由)。

    2026-11-11 22:1x 修一处真缺陷：
      原来的 WAIT 条件是"距我上轮结束 > 30 秒"，而 SILENT（≥2 全牢骚）排在它前面，
      于是**只要 2 条全牢骚就永远判 SILENT，WAIT 一次也走不到 —— 是死代码**。
      自测用例抓到了这一点（见 tools\test_comm_gate.py 反向用例）。
      改法：WAIT 的判据换成**"最新一条距今多久"**（她可能还在补），并排在 BATCH/SILENT 之前。
      这样两者的语义才是分开的：
        WAIT   = 消息还在来，先别动（看**对方**的节奏）
        SILENT = 消息settled了但都是牢骚，不必回（看**内容**）
    """
    n = len(unread)
    if n == 0:
        return "IDLE", "队列空 —— 不动作，等对方"
    cnt = {}
    for m in unread:
        cnt[m["cls"]] = cnt.get(m["cls"], 0) + 1

    # 1) 求助最优先：攒着就等于没帮
    if cnt.get("求助"):
        return "PROCEED", "有【求助】—— 立刻回，别攒"

    # 2) 还在补：最新一条距今不足 WAIT_SECONDS
    newest = max((m["time"] or 0) for m in unread)
    age_s = (now_ms - newest) / 1000.0 if newest else 9e9
    if n >= 2 and age_s < WAIT_SECONDS:
        return "WAIT", ("队列 %d 条，但最新一条只有 %.0f 秒前 —— 她可能还在补，"
                        "先等 %d 秒再看（别急着回半截）" % (n, age_s, WAIT_SECONDS))

    # 3) 有意见：批量处理，一条回复
    if n >= 2 and cnt.get("意见"):
        return "BATCH", "堆了 %d 条且有【意见】—— 全部读完、合并处理、只回一条" % n

    # 4) 全是牢骚：不回
    if n >= 2 and cnt.get("意见", 0) == 0 and cnt.get("牢骚", 0) == n:
        return "SILENT", "堆了 %d 条全是【牢骚】—— 不回；只在结尾带一句「收到，正在改」" % n

    # 5) 明确的继续信号，且没有未处理的意见
    if unread[-1]["cls"] == "继续" and cnt.get("意见", 0) == 0:
        return "PROCEED", "最后一条是【继续】且没有未处理的【意见】—— 进下一步"

    return "PROCEED", "堆了 %d 条、无【求助】无明确指令 —— 建议合并读一次后回一条" % n


def guard_hint() -> str:
    """内部守则（略） §七：这一轮有没有退路。只读、软提示、读不到就返回空串（不阻塞）。"""
    import os as _os
    import subprocess
    import archive_paths as ap
    repo = str(ap.old_root()) if ap.write_mode() == "old" else str(ap.paths.resolve_root(for_write=True)[0])
    git = r"C:\Program Files\Git\cmd\git.exe"
    try:
        if not _os.path.isdir(_os.path.join(repo, ".git")):
            return ""
        p = subprocess.run([git, "-C", repo, "status", "--porcelain"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=8)
    except Exception:  # noqa: BLE001
        return ""
    left = [x for x in (p.stdout or "").splitlines() if x.strip()]
    if not left:
        return "退路：干净（上一轮已 finish）"
    return ("退路：工作区还有 %d 个未提交改动 —— 若这一轮还没收尾属正常；"
            "若上一轮已经汇报过，就是漏了 finish（收工跑 closeout）" % len(left))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="协作节奏闸（内部守则（略） 的道具）")
    ap.add_argument("--session", default="", help="指定会话文件路径")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--sessions", action="store_true", help="只列会话")
    ap.add_argument("--rate", action="store_true",
                    help="看最近一小时各来源的消息频率（内部守则（略） §十 自查；她也能跑）")
    ap.add_argument("--no-guard", action="store_true", help="不打印退路提示")
    ap.add_argument("--show", type=int, default=5, help="附带显示最后 N 条正文（默认 5）")
    args = ap.parse_args(argv)

    if args.sessions:
        for p in find_sessions()[:15]:
            print("%s  %8.1f KB  %s" % (
                time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
                os.path.getsize(p) / 1024, p))
        return 0

    path = args.session
    if not path:
        cands = find_sessions()
        if not cands:
            print("找不到会话文件：%s" % os.path.join(SESSIONS_ROOT, CWD_KEY))
            return 2
        path = cands[0]

    events = read_events(path)
    msgs, last_end = collect(events)

    if args.rate:
        code = report_rate(msgs, int(time.time() * 1000))
        # 飞书助手 2026-11-15 第三轮 review：--rate 在这里就 return，末尾那行 hint 永远走不到；
        # 而她每轮跑的就是 --rate —— 等于提示从来没出现过。所以这个分支里单独补一次。
        if not args.no_guard:
            hint = guard_hint()
            if hint:
                print(hint)
        return code
    now_ms = int(time.time() * 1000)

    # 未读 = 在我最后一轮结束之后进来的
    if last_end:
        unread = [m for m in msgs if (m["seq"] or 0) > (last_end["seq"] or 0)]
    else:
        unread = list(msgs)

    cnt = {}
    for m in unread:
        cnt[m["cls"]] = cnt.get(m["cls"], 0) + 1
    verdict, why = decide(unread, last_end, now_ms)
    last = unread[-1] if unread else None
    age = ((now_ms - last["time"]) / 1000.0) if last and last["time"] else None

    if args.json:
        print(json.dumps({
            "session": path,
            "unread": len(unread),
            "by_class": cnt,
            "last": {
                "at": hhmmss(last["time"]) if last else None,
                "age_sec": round(age, 1) if age is not None else None,
                "source_kind": last["kind"] if last else None,
            },
            "my_last_turn_end": hhmmss(last_end["time"]) if last_end else None,
            "verdict": verdict,
            "reason": why,
            "messages": [{"seq": m["seq"], "at": hhmmss(m["time"]),
                          "cls": m["cls"], "kind": m["kind"],
                          "text": m["text"][:200]} for m in unread[-args.show:]],
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 58)
    print("未读条数：%d" % len(unread))
    print("分类：意见 %d / 牢骚 %d / 继续 %d / 求助 %d" % (
        cnt.get("意见", 0), cnt.get("牢骚", 0), cnt.get("继续", 0), cnt.get("求助", 0)))
    if last:
        print("最后一条：%s（距今 %.0f 秒，来源：%s）" % (
            hhmmss(last["time"]), age or 0,
            "不明(user) —— 港湾投的和用户打的都长这样" if last["kind"] == "user" else last["kind"]))
    else:
        print("最后一条：（无）")
    print("我最后一轮：%s" % (hhmmss(last_end["time"]) if last_end else "（没找到 turn/end）"))
    print("-" * 58)
    print("判定：%s" % verdict)
    print("  %s" % why)
    if unread and args.show > 0:
        print("-" * 58)
        print("最后 %d 条（正文前 90 字）：" % min(args.show, len(unread)))
        for m in unread[-args.show:]:
            print("  [%s %s %s] %s" % (hhmmss(m["time"]), m["cls"], m["seq"],
                                       m["text"][:90].replace("\n", " ")))
    print("=" * 58)
    print("提醒：来源 kind=user 分不清是用户还是飞书助手（内部守则（略） §九 第 1 条未解）。")
    if not args.no_guard:
        hint = guard_hint()
        if hint:
            print(hint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())