# -*- coding: utf-8 -*-
"""传球门自测：11 项，覆盖每条规则 + 兜底 + 参数缺失。
跑法：python tools\\test_pass_gate.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import os

PY = sys.executable
GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pass_gate.py")
SPEC = os.path.join(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "档案库", "工作台", "传球判据.json")

CASES = [
    # (说明, 参数, 期望退出码, 期望结论关键词)
    ("红线 → 找用户", ["--risk", "red"], 10, "交出去"),
    ("红线不受其他事实影响", ["--risk", "red", "--evidence", "pointable", "--owner", "mine"], 10, "交出去"),
    ("别人的身体 → 交", ["--owner", "theirs"], 10, "交出去"),
    ("看不见自己（自己重启）→ 交", ["--blind-spot"], 10, "交出去"),
    ("证据指不出 → 交（unresolved）", ["--evidence", "vague"], 10, "unresolved"),
    ("证据完全拿不到 → 交", ["--evidence", "none"], 10, "交出去"),
    ("试了两次 → 交", ["--tries", "2"], 10, "交出去"),
    ("试了一次还不能交", ["--tries", "1", "--evidence", "pointable", "--risk", "green"], 0, "自己做"),
    ("纯本地只读能指认 → 自己做", ["--risk", "green", "--evidence", "pointable", "--tries", "0"], 0, "自己做"),
    ("不填任何参数 → 保守判交", [], 10, "交出去"),
    ("demo 能跑", ["--demo"], 0, "传球门判定"),
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.exists(SPEC):
        print("FAIL 判据表不存在：" + SPEC)
        return 1
    spec = json.loads(open(SPEC, "rb").read().decode("utf-8-sig"))
    if not spec.get("规则"):
        print("FAIL 判据表里没有规则")
        return 1
    if spec.get("_兜底", "").find("交出去") < 0:
        print("FAIL 兜底原则不是 fail-closed")
        return 1

    bad = 0
    for i, (desc, args, want_code, want_kw) in enumerate(CASES, 1):
        p = subprocess.run([PY, GATE] + args, capture_output=True)
        out = p.stdout.decode("utf-8", errors="replace")
        ok = (p.returncode == want_code) and (want_kw in out)
        print(("PASS" if ok else "FAIL") + " %2d. %s" % (i, desc))
        if not ok:
            bad += 1
            print("      期望 exit=%s 含『%s』；实际 exit=%s" % (want_code, want_kw, p.returncode))
            print("      输出尾部：" + out.strip()[-200:])

    print("\n共 %d 项，%d 项通过，%d 项失败" % (len(CASES), len(CASES) - bad, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
