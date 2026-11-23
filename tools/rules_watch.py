#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则监视：正本一变，就把它刷进各处注入层（不用人记）。

用法：
    python tools/rules_watch.py --once  --target AGENTS.md
    python tools/rules_watch.py --once  --target AGENTS.md --target D:\\proj\\AGENTS.md
    python tools/rules_watch.py --watch --target AGENTS.md --interval 30

做两件事：
1. 比 `rules/core.md` 的摘要与上次记录；变了就对每个目标跑一次 init_rules；
2. 没变也顺手做一次 `--check`，发现本地被手改就报漂移。

状态文件：rules/.sync-state.json（记上次同步的摘要与时间）。
退出码：0 正常；3 有目标漂移且未能修好；2 参数或正本缺失。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CORE = os.path.join(REPO, "rules", "core.md")
STATE = os.path.join(REPO, "rules", ".sync-state.json")
INIT = os.path.join(HERE, "init_rules.py")


def core_digest() -> str:
    src = io.open(CORE, encoding="utf-8").read().strip()
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def load_state() -> dict:
    try:
        return json.loads(io.open(STATE, encoding="utf-8").read())
    except Exception:
        return {}


def save_state(d: dict) -> None:
    io.open(STATE, "w", encoding="utf-8", newline="\n").write(json.dumps(d, ensure_ascii=False, indent=2))


def sync(targets: list[str], force: bool = False) -> int:
    if not os.path.exists(CORE):
        print("找不到规则正本：", CORE)
        return 2
    digest = core_digest()
    state = load_state()
    changed = force or state.get("digest") != digest
    rc = 0
    for t in targets:
        if changed:
            r = subprocess.run([sys.executable, INIT, "--target", t], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            print(r.stdout.strip() or r.stderr.strip())
            rc = rc or r.returncode
        else:
            r = subprocess.run([sys.executable, INIT, "--target", t, "--check"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "").strip()
            print(f"{t}: {out}")
            if r.returncode != 0:
                rc = 3
    if changed and rc == 0:
        save_state({"digest": digest, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "targets": targets})
        print(f"已同步 {len(targets)} 个目标（{digest}）")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="规则正本变动后自动刷进注入层")
    ap.add_argument("--target", action="append", default=[], help="目标文件，可重复")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.target:
        print("至少给一个 --target")
        return 2
    if a.once or not a.watch:
        return sync(a.target, a.force)
    last = None
    while True:
        d = core_digest()
        if d != last:
            last = d
            sync(a.target)
        time.sleep(max(5, a.interval))


if __name__ == "__main__":
    sys.exit(main())