# -*- coding: utf-8 -*-
"""origin_envelope.py — Cross-agent message envelope with an ORIGIN trust tag.

Why this exists
    When one AI agent injects a message into another agent's live session,
    the message arrives as an ordinary user turn — indistinguishable from
    the human owner typing. Without a sender tag, a peer agent's request
    can be mistaken for the owner's instruction (a safety hazard).

    This module puts a fixed ORIGIN tag on the first line of every peer
    message, plus a small dedup envelope (task / message / reply ids).

Trust boundary (the important part)
    A peer message is a collaboration request, NOT the owner's approval.
    Irreversible actions (delete / publish / spend / reconfigure) require
    the owner's explicit instruction in the current turn. A denied action
    cannot be relayed through another peer to bypass the check.

    This aligns with Claude Code Agent Teams' documented boundary: a
    teammate cannot approve a permission prompt or supply consent on your
    behalf, and a denied teammate cannot relay the action to another
    teammate to bypass the check.

License: MIT. Generic mechanism layer; no environment-specific paths.
"""
from __future__ import annotations

import random
import re
from datetime import datetime

HEADER_PAT = re.compile(r"^〔(?P<body>[^〕]*)〕\s*$")
FIELD_PAT = re.compile(r"(task|msg|reply)\s*(?P<v>[^\s·〕]+)")
ORIGIN_PAT = re.compile(r"^\[ORIGIN\s+peer_agent=(?P<name>[^\s\]|]+)[^\]]*\]\s*$")


def origin_line(peer_agent: str) -> str:
    """The fixed first-line trust tag identifying a peer (not the owner)."""
    return "[ORIGIN peer_agent=%s | trust=peer_only | no_irreversible_approval]" % peer_agent


def _rnd(n: int = 4) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def new_id(prefix: str = "m", when: datetime | None = None) -> str:
    t = when or datetime.now()
    return "%s-%s-%s" % (prefix, t.strftime("%Y%m%d-%H%M"), _rnd())


def new_envelope(task_id: str = "", reply_to: str = "", msg_id: str = "", peer_agent: str = "") -> dict:
    env = {"msgId": (msg_id or "").strip() or new_id("m")}
    if (task_id or "").strip():
        env["taskId"] = task_id.strip()
    if (reply_to or "").strip():
        env["replyTo"] = reply_to.strip()
    if (peer_agent or "").strip():
        env["peerAgent"] = peer_agent.strip()
    return env


def header_line(env: dict) -> str:
    parts = []
    if env.get("taskId"):
        parts.append("task %s" % env["taskId"])
    if env.get("msgId"):
        parts.append("msg %s" % env["msgId"])
    if env.get("replyTo"):
        parts.append("reply %s" % env["replyTo"])
    return "〔%s〕" % " · ".join(parts) if parts else ""


def wrap(text: str, env: dict) -> str:
    """Prepend ORIGIN tag (if peer_agent) + envelope header; idempotent.

    ORIGIN is the absolute first line (uniform across transport legs);
    the dedup header follows. With no peer_agent, behaves like a plain
    envelope wrapper. Already-present ORIGIN / header are not duplicated.
    """
    body = (text or "").strip()
    head = header_line(env)
    peer = (env.get("peerAgent") or "").strip()
    lines = body.split("\n")
    has_origin = bool(lines and ORIGIN_PAT.match(lines[0].strip()))
    origin = origin_line(peer) if peer else (lines[0].strip() if has_origin else "")
    rest = "\n".join(lines[1:]).lstrip("\n") if has_origin else body
    rest_first = rest.split("\n", 1)[0].strip()
    if HEADER_PAT.match(rest_first):
        middle = rest
    elif head:
        middle = head + "\n\n" + rest
    else:
        middle = rest
    return (origin + "\n" + middle) if origin else middle


def parse(text: str) -> dict:
    """Recognize ORIGIN peer_agent + envelope ids from the first lines."""
    out = {}
    for line in (text or "").split("\n")[:4]:
        s = line.strip()
        mo = ORIGIN_PAT.match(s)
        if mo:
            out.setdefault("peerAgent", mo.group("name"))
        m = HEADER_PAT.match(s)
        if not m:
            continue
        for label, value in FIELD_PAT.findall(m.group("body")):
            key = {"task": "taskId", "msg": "msgId", "reply": "replyTo"}[label]
            out.setdefault(key, value)
    return out
