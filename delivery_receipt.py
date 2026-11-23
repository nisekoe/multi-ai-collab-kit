# -*- coding: utf-8 -*-
"""投递回执（G-02，2026-11-18）：说清"送到没"，以及"没送到是为什么"。

为什么要有它
    两个窗口之间没有共享记忆。一条消息只有两种结局：对方拿到了，或没拿到，中间没有
    "大概收到了"。回执就是这条界线上的凭证。
    只记"交没交"不够：404 是对方没开窗、409 是两个桌面窗口同时开着、410 是目标刚关掉、
    401 是口令错、status=0 是连接根本没通——对应的动作完全不同（叫醒／关一个窗／别重试／
    重试一次）。分不出来就只能瞎试，还容易重复发。

两段
    resolve：解析目标就失败，还轮不到发；原因是通道给的 error（no_target／ambiguous／…）。
    send：发送那一步失败；原因由 HTTP 码加插件 error 决定。
    stage 字段区分这两段，修的时候知道该先看哪。

口径（用户 2026-11-18 定）
    连失败原因一起记。分类名直接用插件／通道里已有的词，不另造没人用过的新码。
    不记正文、不记 token；不自动重发——重发是人的决定，回执只管说清结果。

上限
    recent 留 200 条（原来是 20 条，翻不了旧账）；超了丢最老的，last 永远是最新一条。

依据
    自拟模块：原因名取自 harbor 插件与通道的既有返回（no_target／ambiguous／target_not_live／
    identity_unavailable／harbor_not_loaded 等）和 HTTP 语义（401／404／409／410／422）。
    保留 200 条按"一天十几次 × 十来天"估，够翻旧账，又不至于让文件膨胀。
    未经外部验证（没找到现成的投递回执标准）。

退出码
    0 正常／查询成功；2 读不到回执文件或参数不对。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import safe_write as SW  # noqa: E402
import archive_paths  # noqa: E402

KEEP = 200

_DEFAULTS = {
    "feishu_bot": str(archive_paths.resolve("工作台/飞书助手投递回执.json", for_write=True)),
    "desktop_bot": str(archive_paths.resolve("工作台/桌面助手投递回执.json", for_write=True)),
}
_ENV = {"feishu_bot": "FEISHU_REACH_RECEIPT", "desktop_bot": "DESKTOP_REACH_RECEIPT"}

# 发送阶段：HTTP 码直接给出的原因
BY_HTTP = {
    401: "unauthorized",
    404: "no_target",
    409: "ambiguous",
    410: "target_not_live",
    422: "identity_unavailable",
}

# 解析阶段：通道／插件已有的 error 词，归一到同一套名字
RESOLVE_ALIAS = {
    "no session": "no_target",
    "no_target": "no_target",
    "ambiguous": "ambiguous",
    "target_not_live": "target_not_live",
    "target_unconfirmed": "target_unqualified",
    "target_role_mismatch": "target_unqualified",
    "target_delegated": "target_unqualified",
    "identity_conflict": "target_unqualified",
    "binding_invalid": "target_unqualified",
    "identity_unavailable": "identity_unavailable",
    "identity_refresh_failed": "identity_unavailable",
    "harbor_not_loaded": "harbor_not_loaded",
    "origin_unavailable": "origin_unavailable",
    "timeout": "timeout",
}

KNOWN = {
    "accepted", "not_accepted", "no_target", "ambiguous", "target_not_live",
    "target_unqualified", "unauthorized", "identity_unavailable",
    "connection_failed", "origin_unavailable", "harbor_not_loaded", "timeout",
    "unknown",
}


def receipt_path(which: str = "feishu_bot", path=None) -> Path:
    if path:
        return Path(path)
    if which not in _DEFAULTS:
        raise ValueError("which 只能是 %s" % "／".join(sorted(_DEFAULTS)))
    return Path(os.environ.get(_ENV[which], _DEFAULTS[which]))


def classify(http, resp=None):
    """发送阶段：返回 (ok, accepted, result_known, reason)。"""
    resp = resp if isinstance(resp, dict) else {}
    accepted = resp.get("accepted")
    if not http:
        return False, None, False, "connection_failed"
    try:
        code = int(http)
    except (TypeError, ValueError):
        return False, None, False, "unknown"
    if code == 200:
        if resp.get("ok") is True and accepted is True:
            return True, True, True, "accepted"
        return False, accepted, True, "not_accepted"
    reason = BY_HTTP.get(code)
    if reason is None:
        err = str(resp.get("error") or "").strip()
        reason = ("rejected:" + err) if err else ("http_%d" % code)
    return False, accepted, True, reason


def normalize(reason) -> str:
    """把通道给的 error 词归一成一套名字；不认识的保留原文（限长），别硬编。"""
    r = str(reason or "").strip()
    if not r:
        return "unknown"
    if r in RESOLVE_ALIAS:
        return RESOLVE_ALIAS[r]
    if r in KNOWN:
        return r
    return r if len(r) <= 40 else "unknown"


# P1-1 证据字段：回执不只记 ok，还要带可核的证据（与交接四字段同一纪律）。
PROOF_WHERE = ("rollout", "targetSessionId", "msgId", "thread")
PROOF_RESULT = ("accepted", "reply_ts", "reply_head", "正文进了", "http")


def evidence_gaps(entry: dict) -> list[str]:
    """ok=True 的回执必须带证据：在哪（where）＋结果凭据（result）。缺哪类报哪类。
    ok=False/未知不要求结果凭据（失败本身就是结论）。只提醒不阻断投递。"""
    if not entry.get("ok"):
        return []
    gaps = []
    if not any(entry.get(k) for k in PROOF_WHERE):
        gaps.append("缺投递落点证据（rollout/targetSessionId/msgId/thread 任一）")
    if not any(entry.get(k) not in (None, "", False) for k in PROOF_RESULT):
        gaps.append("缺结果凭据（accepted/reply_ts/正文进了/http 任一）")
    return gaps


def load(path=None, which="feishu_bot"): 
    p = receipt_path(which, path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def record(which: str, entry: dict, path=None) -> bool:
    """记一条回执。写不进去会说清楚，但不抛异常——回执不该让投递本身崩掉。"""
    p = receipt_path(which, path)
    row = dict(entry)
    row.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
    row.setdefault("stage", "send")
    row.setdefault("reason", "unknown")
    row.setdefault("result_known", True)
    gaps = evidence_gaps(row)
    if gaps:
        row["evidence_gaps"] = gaps
    old = load(path=p)
    rows = old.get("recent") if isinstance(old.get("recent"), list) else []
    rows = [r for r in rows if isinstance(r, dict)]
    rows.append(row)
    try:
        SW.write_text(str(p), json.dumps({"last": row, "recent": rows[-KEEP:]},
                                         ensure_ascii=False, indent=1) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        print("! 回执没写进去（%s: %s）——投递结果可能是已知的，但这次没有留下记录。"
              % (type(exc).__name__, exc))
        return False


def _fmt(row: dict) -> str:
    return ("%s  %-7s %-20s http=%-4s %s"
            % (row.get("at", "?"), row.get("stage") or "—",
               row.get("reason") or "—", row.get("http", "-"),
               (row.get("targetSessionId") or row.get("detail") or "")[:56]))


def show(which="feishu_bot", n=10, path=None, as_json=False):
    data = load(path=path, which=which)
    rows = [r for r in (data.get("recent") or []) if isinstance(r, dict)][-n:]
    if as_json:
        print(json.dumps({"path": str(receipt_path(which, path)), "rows": rows},
                         ensure_ascii=False, indent=1))
        return 0
    p = receipt_path(which, path)
    print("回执：%s（共 %d 条，显示最近 %d 条）"
          % (p, len(data.get("recent") or []), len(rows)))
    if not rows:
        print("（还没有记录）")
        return 0
    for row in rows:
        print(_fmt(row))
    return 0


def audit(which="feishu_bot", n=10, path=None) -> int:
    """P1-1 证据审计：ok=True 的回执必须带证据；缺的标出（不阻断，提醒补）。"""
    data = load(path=path, which=which)
    rows = [r for r in (data.get("recent") or []) if isinstance(r, dict)][-n:]
    bad = [r for r in rows if r.get("ok") and evidence_gaps(r)]
    print("证据审计（%s）：最近 %d 条，ok=True 缺证据 %d 条" % (which, len(rows), len(bad)))
    for r in bad:
        print("  ✗ %s %s → %s" % (r.get("at", "?"), r.get("msgId") or r.get("targetSessionId") or "",
                                  "；".join(evidence_gaps(r))))
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="看投递回执（最近几条＋原因）")
    ap.add_argument("--which", default="", help="feishu_bot｜desktop_bot，缺省两份都看")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--path", default="", help="直接指定回执文件（测试用）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--audit", action="store_true", help="审最近回执：ok=True 却缺证据的标出（P1-1）")
    args = ap.parse_args(argv)
    targets = [args.which] if args.which else ["feishu_bot", "desktop_bot"]
    rc = 0
    for w in targets:
        if not receipt_path(w, args.path or None).exists() and not args.path:
            print("（没有这份回执：%s）" % receipt_path(w))
            continue
        rc = show(w, max(1, args.n), path=args.path or None, as_json=args.json) or rc
        if args.audit:
            rc = audit(w, max(1, args.n), path=args.path or None) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
