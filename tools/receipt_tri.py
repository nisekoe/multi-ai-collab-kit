#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投递回执三态：refused / queued / consumed / unknown。

为什么需要
    「退出码 0」和「HTTP accepted=true」都只说明传输层收下了，不说明对方读到。
    把三者混着报，人就会以为已经送达，然后重复发或漏跟。

四个状态
    refused   明确被拒（有确定的拒绝证据：硬拒、鉴权失败、目标不合格）
    queued    传输层受理，但没有「进入对方会话」的证据（排队、留言、异步）
    consumed  有「进入对方会话」的证据（正文出现在目标的记录里，或出现对应新回复）
    unknown   查不到证据（超时、连接断了、没回执）——不许猜成 queued

用法
    state, reason = judge(accepted=True, landed=False)        -> queued
    state, reason = judge(accepted=True, landed=True)         -> consumed
    state, reason = judge(error="no_target")                  -> refused
    state, reason = judge(timeout=True)                       -> unknown

依据：GPT 2026-11-23 审校「三态再加未知」；状态名与判据为本机约定，未经外部验证。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATES = ("refused", "queued", "consumed", "unknown")
HISTORY = Path(os.environ.get("DELIVERY_HISTORY")
               or ((Path(os.environ.get("LOCALAPPDATA") or Path.home())
                    / ".multi-ai-collab-kit") / "投递历史.jsonl"))


def judge(*, exit_code: int = 0, accepted: bool | None = None, landed: bool | None = None,
          reply: bool = False, error: str | None = None, timeout: bool = False,
          reason: str = "") -> tuple[str, str]:
    """把一次投递的事实压成 (状态, 依据)。参数都只描述「看到了什么」。"""
    if timeout:
        return "unknown", reason or "timeout：没等到回执"
    if error:
        return "refused", f"拒绝证据：{error}"
    if accepted is False:
        return "refused", reason or "传输层明确未受理"
    if landed is True:
        return "consumed", ("正文已进入目标会话" + ("，且出现了新回复" if reply else ""))
    if landed is False and accepted:
        return "queued", reason or "传输层已受理，但未在目标会话里看到正文"
    if exit_code in (4, 5, 6):
        return "refused", reason or f"退出码 {exit_code}"
    if accepted is True:
        return "queued", reason or "传输层已受理，尚无进入会话的证据"
    return "unknown", reason or "没有可用证据"


def log(leg: str, target: str, state: str, reason: str, **extra) -> None:
    """追加一行历史；失败不影响主流程。"""
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "leg": leg,
               "target": target, "state": state, "reason": reason}
        row.update(extra or {})
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def say(state: str, reason: str) -> str:
    """给人看的一行。"""
    label = {"refused": "被拒", "queued": "已排队（对方尚未读到）",
             "consumed": "已送达（进入对方会话）", "unknown": "结果未知"}.get(state, state)
    return f"回执：{label}｜{reason}"


if __name__ == "__main__":
    import sys
    cases = [
        dict(accepted=True, landed=True, reply=True),
        dict(accepted=True, landed=False),
        dict(accepted=True, landed=True),
        dict(error="no_target"),
        dict(timeout=True),
    ]
    for c in cases:
        st, r = judge(**c)
        print(c, "->", st, "|", r)
    sys.exit(0)