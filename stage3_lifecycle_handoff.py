# -*- coding: utf-8 -*-
"""stage3_lifecycle_handoff.py — Stage 3: Project closeout retirement hook & rebind context handoff (v0.4).

Hardened against GPT round-2 audit:
1. Closeout precondition: retire_slots_for_closed_project() reads active_projects.json on the
   pinned writable root first; raises SlotConfigError on corrupt config and raises
   SlotValidationError(reason='project_still_active') if project_id has NOT been removed yet.
2. Actual header-block check: prepend_handoff_header() inspects ONLY the first non-empty line
   of the business body (after optional [ORIGIN ...] and 〔...〕 envelope lines) instead of
   substring-matching the entire text, so ordinary prose quoting '[战情室接棒指针' still gets
   its real handoff header prepended while genuine headers remain idempotent.
3. Unified trusted confirmation gate (#3): confirm_rebind_and_deliver_with_handoff() ONLY
   accepts a registered HumanConfirmation issued via project_slots.issue_human_confirmation().
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

import picker_ranker as pr
import picker_slots_orchestrator as orch
import project_slots as ps

WAR_ROOM_CANDIDATES = ("CONTEXT.md", "README.md", "入口.md")
HANDOFF_MARKER = "[战情室接棒指针"

ORIGIN_PAT = re.compile(r"^\[ORIGIN\s+peer_agent=[^\s\]|]+[^\]]*\]\s*$")
ENVELOPE_PAT = re.compile(r"^〔[^〕]*〕\s*$")


def retire_slots_for_closed_project(
    store: ps.SlotStore,
    project_id: str,
    *,
    reason: str = "project_closed",
    clean_signals: bool = True,
) -> dict[str, Any]:
    """Unified closeout hook on a single pinned writable root.

    Precondition: project_id MUST already be removed from 00-config/active_projects.json.
    Config errors abort immediately without mutating _state/project_slots.json.
    """
    pid = ps._nonempty_str(project_id)
    if not pid:
        raise ps.SlotValidationError("project_id 不能为空", exit_code=ps.EXIT_ENV, reason="invalid_project_id")

    pinned = store.pinned(for_write=True)
    # Precondition check on the exact same pinned root
    active_projects = pinned.load_active_projects()
    if pid in active_projects:
        raise ps.SlotValidationError(
            "项目 %s 仍在 00-config/active_projects.json 活跃表中，请先摘除再执行收尾回收" % pid,
            exit_code=ps.EXIT_UNQUALIFIED,
            reason="project_still_active",
        )

    retired_keys = pinned.retire_project(pid, reason=reason)
    signals_cleaned = False
    if clean_signals:
        sig_store = pr.SignalStore(pinned)
        sig_doc = sig_store.load()
        changed = False
        if pid in (sig_doc.get("pick_counts") or {}):
            del sig_doc["pick_counts"][pid]
            changed = True
        if pid in (sig_doc.get("dismissed_for_project") or {}):
            del sig_doc["dismissed_for_project"][pid]
            changed = True
        if changed:
            sig_store.save(sig_doc)
            signals_cleaned = True

    return {
        "ok": True,
        "project_id": pid,
        "retired_slots": retired_keys,
        "signals_cleaned": signals_cleaned,
    }


def audit_slot_lifecycle(store: ps.SlotStore) -> dict[str, Any]:
    """Read-only health check on a single pinned read root."""
    pinned = store.pinned(for_write=False)
    projects = pinned.load_active_projects()
    doc = pinned.load_slots_doc()
    counts = {"active": 0, "stale": 0, "retired": 0, "other": 0}
    orphans: list[str] = []

    for key, entry in (doc.get("slots") or {}).items():
        if not isinstance(entry, dict):
            continue
        st = (entry.get("status") or "").strip().casefold()
        if st in counts:
            counts[st] += 1
        else:
            counts["other"] += 1
        pid = ps._nonempty_str(entry.get("project_id")) or (key.split(":", 1)[0] if ":" in key else "")
        if pid and pid not in projects and st != "retired":
            orphans.append(key)

    return {
        "ok": True,
        "active_projects": sorted(projects.keys()),
        "counts": counts,
        "orphan_slots": sorted(orphans),
    }


def _probe_war_room_entry(store: ps.SlotStore, project_rel_path: str) -> str:
    rel_dir = (project_rel_path or "").strip().replace("\\", "/").strip("/")
    if not rel_dir:
        return ""
    for fname in WAR_ROOM_CANDIDATES:
        candidate_rel = "%s/%s" % (rel_dir, fname)
        try:
            p = store.resolve_hub_path(candidate_rel, for_write=False)
            if p.is_file():
                return candidate_rel
        except ps.SlotError:
            continue
    return rel_dir + "/"


def _probe_role_profile(store: ps.SlotStore, role_decl: dict[str, Any]) -> str:
    prof_rel = (role_decl.get("profile") or "").strip().replace("\\", "/").strip("/")
    if not prof_rel:
        return ""
    md_rel = prof_rel if prof_rel.endswith(".md") else ("%s/profile.md" % prof_rel)
    try:
        p = store.resolve_hub_path(md_rel, for_write=False)
        if p.is_file():
            return md_rel
    except ps.SlotError:
        pass
    return prof_rel + "/"


def build_handoff_context_header(
    store: ps.SlotStore,
    project_id: str,
    role: str,
    *,
    previous_target_id: str = "",
    handoff_kind: str = "rebind",
) -> str:
    pinned = store.pinned(for_write=False)
    projects = pinned.load_active_projects()
    platforms = pinned.load_platform_registry()
    proj = projects.get(project_id)
    if proj is None:
        raise ps.SlotValidationError(
            "未找到活跃项目：%s" % project_id, exit_code=ps.EXIT_NO_TARGET, reason="unknown_project"
        )
    role_decl = platforms.get(role) or {}
    rev = (proj.get("project_rev") or "").strip()
    war_room = _probe_war_room_entry(pinned, proj.get("path") or "")
    profile = _probe_role_profile(pinned, role_decl)

    mode_label = "首次绑定" if handoff_kind == "first_bind" else "换窗接棒"
    rev_part = (" | rev=%s" % rev) if rev else ""
    prev_part = (" | 前窗=%s" % previous_target_id[:8]) if previous_target_id else ""

    lines = ["%s | %s:%s | %s%s%s]" % (HANDOFF_MARKER, project_id, role, mode_label, rev_part, prev_part)]
    if war_room:
        lines.append("项目战情室：%s" % war_room)
    if profile:
        lines.append("角色身份正本：%s（按 AGENTS.md 防串台先只读核对）" % profile)
    return "\n".join(lines)


def _has_actual_handoff_header(rest_body: str) -> bool:
    """Check whether business body actually starts with a handoff header block (not inline quote)."""
    for line in (rest_body or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        return s.startswith(HANDOFF_MARKER + " |") and s.endswith("]")
    return False


def prepend_handoff_header(text: str, header: str) -> str:
    """Insert handoff header AFTER optional [ORIGIN ...] / 〔...〕 lines; checks actual header block."""
    body = (text or "").strip()
    hdr = (header or "").strip()
    if not hdr:
        return body
    if not body:
        return hdr

    lines = body.split("\n")
    prefix: list[str] = []
    idx = 0
    if idx < len(lines) and ORIGIN_PAT.match(lines[idx].strip()):
        prefix.append(lines[idx])
        idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
    if idx < len(lines) and ENVELOPE_PAT.match(lines[idx].strip()):
        prefix.append(lines[idx])
        idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1

    rest = "\n".join(lines[idx:]).lstrip("\n")
    if _has_actual_handoff_header(rest):
        return body

    if prefix:
        head_block = "\n".join(prefix)
        return "%s\n\n%s\n\n%s" % (head_block, hdr, rest) if rest else "%s\n\n%s" % (head_block, hdr)
    return "%s\n\n%s" % (hdr, rest) if rest else hdr


def confirm_rebind_and_deliver_with_handoff(
    store: ps.SlotStore,
    project_id: str,
    role: str,
    selected_candidate: Any,
    text: str,
    dispatch_fn: Callable[..., tuple[int, dict[str, Any]]],
    *,
    confirmation: ps.HumanConfirmation,
    allow_paused: bool = False,
    dry_run: bool = True,
    attach_handoff: bool = True,
) -> tuple[int, dict[str, Any]]:
    """Human-confirmed bind/rebind delivery using the same trusted confirmation gate as Stage 1."""
    mutate = not dry_run
    tkey = ps.candidate_key(selected_candidate)
    try:
        conf = ps.validate_human_confirmation(
            confirmation, project_id=project_id, role=role, expected_target_key=tkey
        )
    except ps.SlotError as exc:
        return exc.exit_code, {"ok": False, "bound": False, "reason": exc.reason, "message": str(exc)}

    final_text = text
    handoff_attached = False
    handoff_kind = "same_window"
    prev_id = ""

    try:
        pinned = store.pinned(for_write=mutate)
    except ps.SlotError as exc:
        return exc.exit_code, {"ok": False, "bound": False, "reason": exc.reason, "message": str(exc)}

    if attach_handoff:
        try:
            doc = pinned.load_slots_doc()
            slot_key = ps.make_slot_key(project_id, role)
            existing = doc.get("slots", {}).get(slot_key)

            if not isinstance(existing, dict) or not existing.get("target"):
                handoff_kind = "first_bind"
            else:
                old_t = existing.get("target") or {}
                old_same = (
                    ps.normalize_leg(old_t.get("leg", "")) == tkey[0]
                    and (old_t.get("hostId") or "").strip() == tkey[1]
                    and (old_t.get("id") or "").strip() == tkey[2]
                    and existing.get("status") == "active"
                )
                if not old_same:
                    handoff_kind = "rebind"
                    prev_id = (old_t.get("id") or "").strip()

            if handoff_kind in ("first_bind", "rebind"):
                hdr = build_handoff_context_header(
                    pinned,
                    project_id,
                    role,
                    previous_target_id=prev_id,
                    handoff_kind=handoff_kind,
                )
                final_text = prepend_handoff_header(text, hdr)
                handoff_attached = True
        except ps.SlotError as exc:
            return exc.exit_code, {"ok": False, "bound": False, "reason": exc.reason, "message": str(exc)}

    code, payload = orch.confirm_and_deliver_slot(
        pinned,
        project_id,
        role,
        selected_candidate,
        final_text,
        dispatch_fn,
        confirmation=conf,
        allow_paused=allow_paused,
        dry_run=dry_run,
    )
    payload["handoff_attached"] = handoff_attached
    payload["handoff_kind"] = handoff_kind
    return code, payload
