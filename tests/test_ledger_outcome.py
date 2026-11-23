
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ledger_outcome.py —— ledger_lock 的"结果词"检查自测（含反向用例）

为什么要有它：
  内部守则（略）"完成/成本/生成/变成"全会命中，
  等于**永远不报警**，检查形同虚设（踩坑 §D「写了没核对就报成」的同一个毛病）。
  这里锁住"什么算写对了结果词"。

跑法：python tools\\test_ledger_outcome.py      退出码 0 全过 / 1 有失败
"""
from __future__ import annotations
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import importlib.util
import os
import sys

if __name__ == "__main__":

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    spec = importlib.util.spec_from_file_location("lk", os.path.join(HERE, "ledger_lock.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    CASES = [
        # (正文, 应该有结果词吗, 说明)
        ("[11-11 22:40] 这条什么都没写", False, "没有结果词 → 应该提醒"),
        ("[11-11 22:41] 三处都修好了，成", True, "独立出现的「成」"),
        ("[11-11 22:42] 完成了三处改动", False, "「完成」不是结果词（★关键反向用例）"),
        ("[11-11 22:43] 成本核算过，没成", True, "含「没成」"),
        ("[11-11 22:44] 这件事进行中", True, "「进行中」紧跟汉字也算（中文不空格）"),
        ("[11-11 22:45] 变成两个文件了", False, "「变成」不是结果词"),
        ("[11-11 22:46] 结果：成。", True, "冒号后、句号前"),
        ("[11-11 22:47] 全部通过，成（附证据）", True, "逗号后、括号前"),
        ("[11-11 22:48] 生成了一份报告", False, "「生成」不是结果词"),
    ]

    bad = 0
    for body, want, why in CASES:
        got = m._check_outcome(body)
        ok = got == want
        if not ok:
            bad += 1
        print("  %s %-34s got=%-5s want=%-5s  %s"
              % ("OK " if ok else "FAIL", body[12:46], got, want, why))

    print()
    if bad:
        print("有 %d 项失败" % bad)
        raise SystemExit(1)
    print("全过：%d 项断言" % len(CASES))