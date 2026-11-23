
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_msg_envelope.py —— 信封＋ORIGIN 名牌自测（P1-3，含向后兼容/幂等/反向用例）"""
from __future__ import annotations
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)
import importlib.util, os, sys

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("me", os.path.join(HERE, "msg_envelope.py"))
    me = importlib.util.module_from_spec(spec); spec.loader.exec_module(me)
    fails, cnt = [], [0]
    def ck(name, got, want):
        cnt[0] += 1
        if got != want: fails.append(name)
        print("  %s %-42s got=%s want=%s" % ("OK " if got == want else "FAIL", name, got, want))

    print("=== 一、向后兼容：无 peer_agent 行为同旧版 ===")
    w0 = me.wrap("hello", me.new_envelope())
    ck("无 peer → 首行是信封头", w0.split("\n")[0].startswith("〔消息"), True)
    ck("无 peer → 不含 ORIGIN", "ORIGIN" in w0, False)
    ck("无 peer → 信封头只一个", w0.count("〔消息"), 1)

    print("=== 二、ORIGIN 首行（三腿统一）===")
    w1 = me.wrap("hello", me.new_envelope(peer_agent="review_bot"))
    ck("有 peer → 首行 ORIGIN", w1.split("\n")[0], me.origin_line("review_bot"))
    ck("有 peer → ORIGIN 只一个", w1.count("[ORIGIN"), 1)
    ck("有 peer → 信封头仍在", "〔消息" in w1, True)

    print("=== 三、幂等：已带 ORIGIN/头不重复 ===")
    w2 = me.wrap(me.origin_line("review_bot") + "\nhello", me.new_envelope(peer_agent="review_bot"))
    ck("正文已带 ORIGIN → 不重复", w2.count("[ORIGIN"), 1)
    w3 = me.wrap("〔消息 m-x〕\n\nhello", me.new_envelope())
    ck("正文已带头 → 不重复", w3.count("〔消息"), 1)
    w4 = me.wrap(me.origin_line("gpt") + "\n〔消息 m-y〕\n\nhello", me.new_envelope(peer_agent="gpt"))
    ck("ORIGIN+头都已带 → 各一个", (w4.count("[ORIGIN"), w4.count("〔消息")), (1, 1))

    print("=== 四、parse 认 ORIGIN＋信封 ===")
    p = me.parse(w1)
    ck("parse peerAgent", p.get("peerAgent"), "review_bot")
    ck("parse msgId 在", bool(p.get("msgId")), True)
    p2 = me.parse(me.wrap("hi", me.new_envelope(task_id="T1", peer_agent="gpt")))
    ck("parse taskId", p2.get("taskId"), "T1")
    ck("parse peerAgent gpt", p2.get("peerAgent"), "gpt")

    print()
    if fails:
        print("FAILED %d: %s" % (len(fails), fails)); raise SystemExit(1)
    print("全过：%d 项断言" % cnt[0])
