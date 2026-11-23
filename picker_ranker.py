# -*- coding: utf-8 -*-
"""picker_ranker.py — Unified picker Layer 2: Explainable deterministic ranker & local signal cache (v0.3).

Hardened against GPT cross-leg audit & desktop_bot isolated probes:
- Path decoding strictly matches cxa.fs_path_key rule: ONLY file:// URIs are percent-unquoted;
  raw filesystem paths preserve literal %20 (workspace/a%20b != workspace/a b). Supports UNC & file://host/share.
- Workspace affinity uses full path-segment boundary matching (prevents 'hub' hitting 'github').
- Enforces real host role filtering (expected_host_role: desktop / feishu_bot / peer).
- Strict busy validation via project_slots.inspect_busy_state():
  * busy -> -10 + '⏳ 正忙（慎选）' (excluded from recommendations)
  * idle + active within 24h -> +10 + '空闲活跃'
  * unknown busy state (missing/-1/NaN/bool) + recent lastActivity -> 0 pts + '近期活动（忙态未知）'
- Hard safety gate FIRST: verify_target(c, allow_protected=False) runs BEFORE any scoring.
"""
from __future__ import annotations

import json
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

import project_slots as ps

SIGNALS_SCHEMA_VERSION = "0.1.0"
PICKER_SIGNALS_REL = "_local/cache/picker_signals.json"

SCORE_HISTORY_OR_COLLAB = 35
SCORE_WORKSPACE_AFFINITY = 25
SCORE_TITLE_OR_KEYWORD = 20
SCORE_IDLE_RECENT = 10
PENALTY_BUSY = -10
PENALTY_SUPPRESSED = -25

RECENT_ACTIVITY_WINDOW_SEC = 24 * 3600
RECOMMEND_MIN_SCORE = 40
RECOMMEND_LEAD_MARGIN = 15
RECOMMEND_DUAL_HIGH_SCORE = 60
MAX_RECOMMENDED = 2


@dataclass
class RankedCandidate:
    candidate: Any
    score: int
    raw_score: int
    reasons: list[str] = field(default_factory=list)
    breakdown: dict[str, int] = field(default_factory=dict)
    recommended: bool = False
    is_busy: bool = False
    busy_state: str = "idle"  # "idle" | "busy" | "unknown"
    suppressed: bool = False
    bound_projects: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    return " ".join(s.split()).casefold()


def _norm_path_literal(text: str) -> str:
    """Normalize filesystem paths for segment matching WITHOUT collapsing interior spaces (P5)."""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _decode_path(raw: str) -> str:
    """Normalize paths/URIs via shared project_slots._default_cwd_key (production cxa.fs_path_key).

    Preserves literal raw path %20, restores UNC & file://host/share, and uses forward slashes
    for segment-boundary workspace matching.
    """
    if not raw or not str(raw).strip():
        return ""
    key = ps._default_cwd_key(str(raw))
    if not key:
        return ""
    return key.replace("\\", "/").rstrip("/")


def _path_segments(norm_path: str) -> list[str]:
    if not norm_path:
        return []
    return [seg for seg in norm_path.replace("\\", "/").split("/") if seg]


def _path_matches_token(cwd_norm: str, token_norm: str) -> bool:
    """Match workspace tokens on full path-segment boundaries (prevents 'hub' hitting 'github')."""
    cwd_segs = _path_segments(cwd_norm)
    tok_segs = _path_segments(token_norm)
    if not cwd_segs or not tok_segs or len(tok_segs) > len(cwd_segs):
        return False
    n = len(tok_segs)
    for i in range(len(cwd_segs) - n + 1):
        if cwd_segs[i : i + n] == tok_segs:
            return True
    return False


def _target_key_str(leg: str, host_id: str, target_id: str) -> str:
    return "%s|%s|%s" % (ps.normalize_leg(leg), (host_id or "").strip(), (target_id or "").strip())


def parse_activity_ts(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e12 else v
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if "." in s:
        head, rest = s.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                frac += ch
            else:
                tz = rest[i:]
                break
        s = "%s.%s%s" % (head, frac[:6].ljust(6, "0"), tz)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class SignalStore:
    """Manages regenerable local ranking signals in _local/cache/picker_signals.json."""

    def __init__(
        self,
        store: ps.SlotStore | None = None,
        *,
        hub_root: Path | str | None = None,
        cli_root: str | None = None,
        clock: Callable[[], str] = ps.now_iso,
    ):
        self._slot_store = store or ps.SlotStore(hub_root=hub_root, cli_root=cli_root, clock=clock)
        self._clock = clock

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": SIGNALS_SCHEMA_VERSION,
            "updated_at": self._clock(),
            "pick_counts": {},
            "dismissed_for_project": {},
            "personal_chat_targets": [],
        }

    def load(self) -> dict[str, Any]:
        try:
            path = self._slot_store.resolve_hub_path(PICKER_SIGNALS_REL, for_write=False)
        except ps.SlotError:
            return self._empty()
        if not path.is_file():
            return self._empty()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        base = self._empty()
        if isinstance(data.get("pick_counts"), dict):
            base["pick_counts"] = data["pick_counts"]
        if isinstance(data.get("dismissed_for_project"), dict):
            base["dismissed_for_project"] = data["dismissed_for_project"]
        if isinstance(data.get("personal_chat_targets"), list):
            base["personal_chat_targets"] = [x for x in data["personal_chat_targets"] if isinstance(x, str)]
        return base

    def save(self, doc: dict[str, Any]) -> Path:
        pinned = self._slot_store.pinned(for_write=True)
        payload = {
            "schema_version": SIGNALS_SCHEMA_VERSION,
            "updated_at": self._clock(),
            "pick_counts": doc.get("pick_counts") or {},
            "dismissed_for_project": doc.get("dismissed_for_project") or {},
            "personal_chat_targets": sorted(set(doc.get("personal_chat_targets") or [])),
        }
        path = pinned.resolve_hub_path(PICKER_SIGNALS_REL, for_write=True)
        ps._write_json_atomic(path, payload)
        return path

    def record_pick(self, project_id: str, leg: str, host_id: str, target_id: str) -> dict[str, Any]:
        pinned = self._slot_store.pinned(for_write=True)
        local = SignalStore(pinned, clock=self._clock)
        doc = local.load()
        tkey = _target_key_str(leg, host_id, target_id)
        proj_counts = doc["pick_counts"].setdefault(project_id, {})
        proj_counts[tkey] = int(proj_counts.get(tkey, 0)) + 1
        dismissed = doc["dismissed_for_project"].get(project_id)
        if isinstance(dismissed, list) and tkey in dismissed:
            doc["dismissed_for_project"][project_id] = [x for x in dismissed if x != tkey]
        local.save(doc)
        return doc

    def record_dismissed(self, project_id: str, leg: str, host_id: str, target_id: str) -> dict[str, Any]:
        pinned = self._slot_store.pinned(for_write=True)
        local = SignalStore(pinned, clock=self._clock)
        doc = local.load()
        tkey = _target_key_str(leg, host_id, target_id)
        dismissed = doc["dismissed_for_project"].setdefault(project_id, [])
        if tkey not in dismissed:
            dismissed.append(tkey)
        local.save(doc)
        return doc

    def mark_personal_chat(self, leg: str, host_id: str, target_id: str, *, is_personal: bool = True) -> dict[str, Any]:
        pinned = self._slot_store.pinned(for_write=True)
        local = SignalStore(pinned, clock=self._clock)
        doc = local.load()
        tkey = _target_key_str(leg, host_id, target_id)
        cur = set(doc.get("personal_chat_targets") or [])
        if is_personal:
            cur.add(tkey)
        else:
            cur.discard(tkey)
        doc["personal_chat_targets"] = sorted(cur)
        local.save(doc)
        return doc


def _extract_project_tokens(project_entry: dict[str, Any] | None, project_id: str, keyword: str) -> tuple[list[str], list[str]]:
    title_tokens: list[str] = []
    ws_tokens: list[str] = []

    if project_id:
        title_tokens.append(_norm(project_id))
    if keyword:
        title_tokens.append(_norm(keyword))

    if isinstance(project_entry, dict):
        rel_path = (project_entry.get("path") or "").strip().replace("\\", "/").strip("/")
        if rel_path:
            ws_tokens.append(_norm_path_literal(rel_path))
            leaf = rel_path.split("/")[-1]
            if leaf:
                title_tokens.append(_norm(leaf))
                ws_tokens.append(_norm_path_literal(leaf))
        for kw in project_entry.get("keywords") or []:
            if isinstance(kw, str) and kw.strip():
                title_tokens.append(_norm(kw))
        for hint in project_entry.get("workspace_hints") or []:
            if isinstance(hint, str) and hint.strip():
                ws_tokens.append(_norm_path_literal(_decode_path(hint)))

    title_tokens = [t for t in dict.fromkeys(title_tokens) if t]
    ws_tokens = [t for t in dict.fromkeys(ws_tokens) if t]
    return title_tokens, ws_tokens


def rank_candidates(
    candidates: list[Any],
    *,
    project_id: str = "",
    role: str = "",
    keyword: str = "",
    project_entry: dict[str, Any] | None = None,
    slots_doc: dict[str, Any] | None = None,
    signals_doc: dict[str, Any] | None = None,
    expected_leg: str = "",
    expected_host_id: str = "",
    expected_host_role: str = "",
    hub_root_hint: str = "",
    now_ts: float | None = None,
) -> list[RankedCandidate]:
    """Deterministically filter, score (0..100), explain, and mark recommendations."""
    now = float(now_ts) if now_ts is not None else time.time()
    norm_exp_leg = ps.normalize_leg(expected_leg) if expected_leg else ""
    norm_exp_host = (expected_host_id or "").strip()
    norm_exp_host_role = ps.normalize_host_role(expected_host_role) if expected_host_role else ""

    slots = (slots_doc or {}).get("slots") if isinstance(slots_doc, dict) else {}
    if not isinstance(slots, dict):
        slots = {}

    slot_key = "%s:%s" % (project_id, role) if (project_id and role) else ""
    prev_keys_for_slot: set[str] = set()
    active_projects_by_target: dict[str, list[str]] = {}
    same_role_other_active_keys: set[str] = set()

    for skey, entry in slots.items():
        if not isinstance(entry, dict):
            continue
        e_pid = (entry.get("project_id") or "").strip()
        e_role = (entry.get("role") or "").strip()
        tgt = entry.get("target") or {}
        t_leg = ps.normalize_leg(tgt.get("leg", ""))
        t_host = (tgt.get("hostId") or "").strip()
        t_id = (tgt.get("id") or "").strip()
        if entry.get("status") == "active" and t_leg and t_host and t_id:
            tk = _target_key_str(t_leg, t_host, t_id)
            if e_pid:
                active_projects_by_target.setdefault(tk, []).append(e_pid)
            if role and e_role == role and e_pid != project_id:
                same_role_other_active_keys.add(tk)
        if slot_key and skey == slot_key:
            for p in entry.get("previous_targets") or []:
                if isinstance(p, dict) and p.get("leg") and p.get("hostId") and p.get("id"):
                    prev_keys_for_slot.add(_target_key_str(p["leg"], p["hostId"], p["id"]))

    sig = signals_doc if isinstance(signals_doc, dict) else {}
    dismissed_map = sig.get("dismissed_for_project") if isinstance(sig.get("dismissed_for_project"), dict) else {}
    dismissed_for_proj = set(dismissed_map.get(project_id) or []) if project_id else set()
    personal_set = set(sig.get("personal_chat_targets") or [])

    title_tokens, ws_tokens = _extract_project_tokens(project_entry, project_id, keyword)
    if hub_root_hint:
        h_dec = _norm_path_literal(_decode_path(hub_root_hint))
        if h_dec:
            ws_tokens.append(h_dec)
            leaf = h_dec.split("/")[-1]
            if leaf and leaf not in ws_tokens:
                ws_tokens.append(leaf)

    seen: set[str] = set()
    ranked: list[tuple[RankedCandidate, float]] = []

    project_label = ""
    if isinstance(project_entry, dict) and project_entry.get("path"):
        project_label = str(project_entry["path"]).replace("\\", "/").strip("/").split("/")[-1]
    project_label = project_label or project_id

    for c in candidates:
        c_leg, c_host, c_id = ps.candidate_key(c)
        if not c_leg or not c_host or not c_id:
            continue
        if norm_exp_leg and c_leg != norm_exp_leg:
            continue
        if norm_exp_host and c_host != norm_exp_host:
            continue
        if norm_exp_host_role:
            role_ok, _rr = ps._candidate_matches_host_role(c, norm_exp_leg or c_leg, norm_exp_host_role)
            if not role_ok:
                continue
        tk = _target_key_str(c_leg, c_host, c_id)
        if tk in seen:
            continue

        ok, _reason = ps._verify_candidate(c, allow_protected=False)
        if not ok:
            continue
        seen.add(tk)

        breakdown: dict[str, int] = {}
        reasons: list[str] = []

        # Signal 1: History / associated collab window (+35)
        if tk in prev_keys_for_slot:
            breakdown["history_collab"] = SCORE_HISTORY_OR_COLLAB
            reasons.append("上次「%s」协作窗" % project_label if project_label else "历史协作窗")
        elif tk in same_role_other_active_keys:
            breakdown["history_collab"] = SCORE_HISTORY_OR_COLLAB
            reasons.append("常用协作窗")

        # Signal 2: Workspace affinity (+25) — literal raw path, preserves interior spaces, segment boundary
        cwd_norm = _norm_path_literal(_decode_path(getattr(c, "cwd", "") or ""))
        if cwd_norm and ws_tokens and any(_path_matches_token(cwd_norm, tok) for tok in ws_tokens):
            breakdown["workspace_affinity"] = SCORE_WORKSPACE_AFFINITY
            reasons.append("同工作区")

        # Signal 3: Title / keyword match (+20)
        meta = getattr(c, "meta", None) or {}
        title_haystack = [
            _norm(getattr(c, "display_title", "") or ""),
            _norm(getattr(c, "title", "") or ""),
            _norm(getattr(c, "alias", "") or ""),
            _norm(meta.get("projectName", "") if isinstance(meta, dict) else ""),
        ]
        if title_tokens and any(tok in hay for hay in title_haystack if hay for tok in title_tokens):
            breakdown["title_match"] = SCORE_TITLE_OR_KEYWORD
            reasons.append("标题匹配")

        # Signal 4 & 5: Strict busy state classification
        busy_state, _busy_detail = ps.inspect_busy_state(c)
        is_busy = (busy_state == "busy")
        act_ts = parse_activity_ts(getattr(c, "lastActivity", ""))
        busy_ago = getattr(c, "busySecondsAgo", None)
        recent_by_ts = act_ts is not None and 0 <= (now - act_ts) <= RECENT_ACTIVITY_WINDOW_SEC
        recent_by_ago = (
            busy_state == "idle"
            and isinstance(busy_ago, (int, float))
            and not isinstance(busy_ago, bool)
            and ps.BUSY_WINDOW_SEC <= busy_ago <= RECENT_ACTIVITY_WINDOW_SEC
        )

        if busy_state == "busy":
            breakdown["busy_penalty"] = PENALTY_BUSY
            reasons.append("⏳ 正忙（慎选）")
        elif busy_state == "idle":
            if recent_by_ts or recent_by_ago:
                breakdown["idle_recent"] = SCORE_IDLE_RECENT
                reasons.append("空闲活跃")
        else:
            # busy_state == "unknown": NEVER award +10 '空闲活跃'; only show neutral note if recent
            if recent_by_ts:
                reasons.append("近期活动（忙态未知）")

        # Signal 6: Anti-disturb suppression (-25)
        suppressed = (tk in dismissed_for_proj) or (tk in personal_set)
        if suppressed:
            breakdown["suppressed_penalty"] = PENALTY_SUPPRESSED

        raw_score = sum(breakdown.values())
        score = max(0, min(100, raw_score))
        bound_projs = sorted(set(active_projects_by_target.get(tk, [])))

        rc = RankedCandidate(
            candidate=c,
            score=score,
            raw_score=raw_score,
            reasons=reasons,
            breakdown=breakdown,
            recommended=False,
            is_busy=is_busy,
            busy_state=busy_state,
            suppressed=suppressed,
            bound_projects=bound_projs,
        )
        ranked.append((rc, act_ts if act_ts is not None else -1.0))

    ranked.sort(
        key=lambda item: (
            -item[0].score,
            1 if item[0].is_busy else 0,
            -item[1],
            _norm(getattr(item[0].candidate, "display_title", "") or getattr(item[0].candidate, "title", "") or ""),
            getattr(item[0].candidate, "id", ""),
        )
    )
    ordered = [item[0] for item in ranked]

    eligible = [r for r in ordered if r.score >= RECOMMEND_MIN_SCORE and not r.suppressed and not r.is_busy]
    if len(eligible) == 1:
        eligible[0].recommended = True
    elif len(eligible) >= 2:
        top1, top2 = eligible[0], eligible[1]
        if (top1.score - top2.score) >= RECOMMEND_LEAD_MARGIN:
            top1.recommended = True
        elif top1.score >= RECOMMEND_DUAL_HIGH_SCORE and top2.score >= RECOMMEND_DUAL_HIGH_SCORE:
            if len(eligible) == 2 or (top2.score - eligible[2].score) >= RECOMMEND_LEAD_MARGIN:
                top1.recommended = True
                top2.recommended = True

    return ordered
