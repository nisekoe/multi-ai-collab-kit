#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The single sending entry for the Codex leg.

Why it exists
    Text rules cannot stop a script from calling the transport layer directly.
    This entry is the only supported way to send: it decides whether the target
    was chosen by a human (card), remembered for the task, or handed back as a
    return address, and it refuses a bare thread id.

Usage
    python tools/send_codex.py --task <id> --file body.txt        # reuse the task's chosen thread
    python tools/send_codex.py --ask  --file body.txt             # pop a card and pick
    python tools/send_codex.py --reply-to harbor|local|session --file body.txt
    python tools/send_codex.py --thread <uuid> --confirmed --file body.txt   # human named it explicitly

Exit codes: 0 sent; 2 missing args; 4 would bypass selection; 6 refused by gate.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REACH = os.path.join(HERE, "reach_gpt.py")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="single entry for Codex delivery")
    ap.add_argument("--thread")
    ap.add_argument("--file")
    ap.add_argument("--mode", choices=["sync", "queue"], default="queue")
    ap.add_argument("--task", default="", help="reuse the thread chosen for this task")
    ap.add_argument("--reply-to", default="", help="leg|host|session from the incoming message")
    ap.add_argument("--ask", action="store_true", help="pop a selection card")
    ap.add_argument("--confirmed", action="store_true", help="a human named this thread explicitly")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.reply_to:
        parts = a.reply_to.split("|")
        if len(parts) == 3 and parts[2]:
            a.thread = parts[2]

    if a.thread and not (a.task or a.reply_to or a.confirmed or a.ask):
        print("Refused: a bare --thread bypasses window selection.")
        print("Use --ask (card), --task (remembered choice), --reply-to (return address),")
        print("or let a human name it and pass --confirmed.")
        return 4

    if not a.thread and not a.ask:
        print("Need --thread with --confirmed, --task, --reply-to, or --ask.")
        return 2

    if a.ask and not a.thread:
        print("Pop your selection card first, then call again with --thread <chosen> --confirmed.")
        return 4

    cmd = [sys.executable, REACH, "--thread", a.thread, "--mode", a.mode, "--skip-gate"]
    if a.file:
        cmd += ["--file", a.file]
    if a.dry_run:
        print("dry-run:", " ".join(cmd))
        return 0

    env = dict(os.environ)
    env["DAFEIYU_SEND_ENTRY"] = "1"
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    sys.exit(main())