#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡片服务的「纪律性唤醒」脚本：任何协作发起前先确保它在跑。

规则（按用户 2026-11-23 的要求）：
1. 只负责「确保服务活着」，不负责弹卡；
2. 幂等：已在跑就立刻返回，不重复起；
3. 失败要报原因，不静默；
4. 可被任何脚本 import：ensure() -> (ok, detail)。

退出码：0 在跑/已拉起；1 拉不起来。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import paths  # noqa: E402

BASE = "http://127.0.0.1:%d" % paths.card_port()
PYW = Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else Path(sys.executable)
if not PYW.is_file():
    PYW = Path(sys.executable)
SERVICE = HERE / "card_service.py"
LOG = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
       / ".multi-ai-collab-kit" / "logs" / "card_service_boot.log")


def alive(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(BASE + "/api/pending", timeout=timeout) as response:
            response.read()
        return True
    except Exception:
        return False


def ensure(wait: float = 8.0) -> tuple[bool, str]:
    if alive():
        return True, "already_running"
    if not SERVICE.is_file():
        return False, "service_file_missing"
    exe = str(PYW) if PYW.is_file() else sys.executable
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ensure: starting\n"))
        subprocess.Popen([exe, str(SERVICE)], cwd=str(HERE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception as exc:
        return False, f"spawn_failed: {exc}"
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.6)
        if alive():
            return True, "started"
    return False, "started_but_not_listening"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确保本机选窗卡片服务可用")
    parser.add_argument("--wait", type=float, default=8.0, help="等待服务启动的秒数")
    args = parser.parse_args(argv)
    ok, detail = ensure(args.wait)
    payload = {"ok": ok, "detail": detail, "base": BASE}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())