#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_delivery_receipt.py —— P1-1 回执证据字段自测（含反向用例）"""
from __future__ import annotations
import importlib.util, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("dr", os.path.join(HERE, "delivery_receipt.py"))
dr = importlib.util.module_from_spec(spec); spec.loader.exec_module(dr)
fails, cnt = [], [0]
def ck(n, g, w):
    cnt[0] += 1
    if g != w: fails.append(n)
    print("  %s %-38s got=%s want=%s" % ("OK " if g == w else "FAIL", n, g, w))
print("=== evidence_gaps ===")
ck("ok+落点+凭据 → 无缺口", dr.evidence_gaps({"ok": True, "targetSessionId": "x", "accepted": True}), [])
ck("ok 裸 → 两类缺口", len(dr.evidence_gaps({"ok": True})), 2)
ck("ok+msgId+reply_ts → 无", dr.evidence_gaps({"ok": True, "msgId": "m", "reply_ts": "t"}), [])
ck("ok+rollout+正文进了 → 无", dr.evidence_gaps({"ok": True, "rollout": "p", "正文进了": True}), [])
ck("ok=False → 不要求", dr.evidence_gaps({"ok": False}), [])
ck("ok+落点缺凭据 → 1", len(dr.evidence_gaps({"ok": True, "msgId": "m"})), 1)
ck("ok+http200 → 凭据足", dr.evidence_gaps({"ok": True, "msgId": "m", "http": 200}), [])
print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails)); raise SystemExit(1)
print("全过：%d 项" % cnt[0])
