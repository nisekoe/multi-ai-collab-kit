#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡片服务的「纪律性唤醒」脚本（已接入 service_ensure 统一底座）。

规则：
1. 只负责「确保服务活着」，不负责弹卡；
2. 幂等：已在跑且为真实桌面实例就立刻返回，不重复起；
3. 严禁直接 Popen：统一走 service_ensure.ensure("card_service") 经宿主机交互式计划任务拉起；
4. 失败必报明确原因，不静默；
5. 保持原有 alive() / ensure(wait=8.0) 与 CLI JSON 输出完全兼容。

退出码：0 在跑/已拉起；1 拉不起来。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import paths  # noqa: E402
import service_ensure  # noqa: E402

BASE = "http://127.0.0.1:%d" % paths.card_port()


def alive(timeout: float = 2.0) -> bool:
    ok, _ = service_ensure.check("card_service", timeout=timeout)
    return ok


def ensure(wait: float = 8.0) -> tuple[bool, str]:
    return service_ensure.ensure("card_service", wait=wait)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="确保本机选窗卡片服务可用（走宿主机交互式计划任务）")
    parser.add_argument("--wait", type=float, default=8.0, help="等待服务启动的秒数")
    args = parser.parse_args(argv)
    ok, detail = ensure(args.wait)
    payload = {"ok": ok, "detail": detail, "base": BASE}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
