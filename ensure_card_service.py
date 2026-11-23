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

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8790"
HERE = Path(__file__).resolve().parent
PYW = Path(r"F:\program\deepseek-dafeiyu\uv\tools\astrbot\Scripts\pythonw.exe")
SERVICE = HERE / "card_service.py"
LOG = Path(r"F:\hub\_local\_tmp\card_service_boot.log")


def alive(timeout: float = 2.0) -> bool:
    try:
        urllib.request.urlopen(BASE + "/api/pending", timeout=timeout).read()
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
    ok, detail = ensure()
    payload = {"ok": ok, "detail": detail, "base": BASE}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())