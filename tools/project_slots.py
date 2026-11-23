# -*- coding: utf-8 -*-
"""project_slots.py — Unified picker Layer 1: Project x Role slot routing & ledger (v0.6).

Hardened against GPT v0.5 final review & probe_v05.py:
1. Trusted host-only confirmation & single-use dispatch binding (#1):
   - `issue_confirmation_from_cli_tty()` uses ONLY real process `sys.stdin.isatty()` and
     real interactive `sys.stdin.readline()` — NO injectable `isatty_fn` or `input_fn` hooks
     in production API (tests mock sys.stdin strictly at the test boundary).
   - `HumanConfirmation` is single-use per dispatch: `claim_human_confirmation()` atomically
     marks the confirmation consumed under lock before dispatch so reusing the same token
     for a second dispatch/bind is strictly rejected (`confirmation_already_used`, 0 posts).
   - `HumanConfirmation` carries immutable render-time `frozen_cwd` / `has_frozen_cwd` /
     `frozen_role` from the host-issued card snapshot (#2b).
2. Monotonic slot_version + strict absent-vs-existing CAS (#3):
   - Every slot mutation (bind / mark_slot_stale / retire) increments monotonic `slot_version`.
   - Distinguishes "row does not exist" (`expected_prior_version=0`, requires `live_entry is None`)
     from ANY existing row (`active`, `stale`, or `retired`). If concurrent confirmation B binds
     and later turns `stale` while older confirmation A (started from absent) is in flight,
     A's late commit detects `live_entry is not None` / version mismatch and preserves B + its
     `stale` status untouched.
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_TOOLS_DIR = Path(os.environ.get("HUB_TOOLS_DIR") or Path(__file__).resolve().parent)
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

SCHEMA_VERSION = "0.4.0"
ACTIVE_PROJECTS_REL = "00-config/active_projects.json"
PLATFORM_REGISTRY_REL = "00-config/平台注册表.json"
PROJECT_SLOTS_REL = "_state/project_slots.json"

ALLOWED_BOUND_BY = frozenset({"human_card", "human_cli"})
LEG_TO_EVIDENCE_TYPE = {
    "porta": "porta_verified_step",
    "codex": "codex_reach_gpt_verified",
    "harbor": "harbor_say_accepted_receipt",
}
ALLOWED_EVIDENCE_TYPES = frozenset(LEG_TO_EVIDENCE_TYPE.values())
VALID_SLOT_STATUSES = frozenset({"active", "stale", "retired"})
BUSY_STATUSES = frozenset({"running", "busy_recent"})
BUSY_WINDOW_SEC = 60
MAX_PREVIOUS_TARGETS = 10

LEG_ALIASES = {
    "dsh-harbor": "harbor",
    "harbor": "harbor",
    "porta": "porta",
    "codex": "codex",
}

REGISTRY_TO_HOST_ROLE = {
    "desktop": "desktop",
    "feishu": "feishu_bot",
    "feishu_bot": "feishu_bot",
    "peer": "peer",
}

AUTHORITATIVE_STALE_REASONS = frozenset({
    "archived",
    "unloaded_disk_only",
    "cloud_no_rollout",
    "no_local_record",
    "workspace_protected",
    "cwd_drift",
    "role_mismatch",
    "platform_mismatch",
    "host_mismatch",
})

TRANSIENT_SOURCE_REASONS = frozenset({
    "state_unavailable",
    "source_incomplete",
    "registry_unavailable",
    "registry_invalid",
    "registry_incomplete",
    "catalog_unavailable",
    "porta_unhealthy",
    "porta_truncated",
    "porta_host_uncovered",
    "identity_missing",
    "post_receipt_mismatch",
    "no_target",
    "bad_thread_uuid",
    "body_unreadable",
    "body_empty",
    "env_error",
    "unreachable",
    "timeout",
    "not_consumed",
    "commit_timeout",
    "porta_step_association_missing",
    "codex_receipt_missing",
    "reverify_error",
})

EXIT_OK = 0
EXIT_ENV = 2
EXIT_NO_TARGET = 3
EXIT_AMBIGUOUS = 4
EXIT_VERIFY_FAIL = 5
EXIT_UNQUALIFIED = 6
EXIT_BUSY = 7
EXIT_CANCELLED = 10

_HOST_GATE_SECRET = uuid.uuid4().hex
_UNSET = object()

_LOCK_TABLE_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCK_STATE = threading.local()

# Single-use confirmation registry protected by dedicated lock (indexed by unique issuer_token)
_CONFIRMATION_LOCK = threading.Lock()
_ISSUED_CONFIRMATIONS: dict[str, "HumanConfirmation"] = {}
_CLAIMED_CONFIRMATION_TOKENS: set[str] = set()


def _get_path_lock(path: Path) -> tuple[str, threading.RLock]:
    key = os.path.normcase(str(path.resolve()))
    with _LOCK_TABLE_GUARD:
        lk = _PATH_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _PATH_LOCKS[key] = lk
        return key, lk


@contextmanager
def _exclusive_ledger_lock(slots_path: Path, timeout_sec: float = 5.0):
    """Cross-thread and cross-process exclusive lock (re-entrant within the same thread)."""
    key, rlock = _get_path_lock(slots_path)
    depths = getattr(_THREAD_LOCK_STATE, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCK_STATE.depths = depths

    if depths.get(key, 0) > 0:
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    lock_path = slots_path.with_name(slots_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with rlock:
        fh = open(lock_path, "a+b")
        locked = False
        try:
            deadline = time.monotonic() + timeout_sec
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SlotError("槽位账本锁等待超时：%s" % lock_path, exit_code=EXIT_ENV, reason="ledger_lock_timeout")
                    time.sleep(0.01)
            depths[key] = 1
            try:
                yield
            finally:
                depths[key] = 0
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            fh.close()


class SlotError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_ENV, reason: str = "slot_error"):
        super().__init__(message)
        self.exit_code = exit_code
        self.reason = reason


class SlotConfigError(SlotError):
    def __init__(self, message: str, reason: str = "config_invalid"):
        super().__init__(message, exit_code=EXIT_ENV, reason=reason)


class SlotPermissionError(SlotError):
    def __init__(self, message: str, reason: str = "origin_forbidden"):
        super().__init__(message, exit_code=EXIT_UNQUALIFIED, reason=reason)


class SlotValidationError(SlotError):
    def __init__(self, message: str, exit_code: int = EXIT_UNQUALIFIED, reason: str = "validation_failed"):
        super().__init__(message, exit_code=exit_code, reason=reason)


class SlotConcurrencyError(SlotError):
    """Raised when a CAS write detects concurrent mutation or unexpected existing row."""

    def __init__(self, message: str, current_entry: dict[str, Any] | None = None):
        super().__init__(message, exit_code=EXIT_UNQUALIFIED, reason="slot_cas_conflict")
        self.current_entry = current_entry


@dataclass(frozen=True)
class VerifiedHostEvent:
    actor: str  # "human_card" | "human_cli"
    project_id: str
    role: str
    target_key: tuple[str, str, str]
    confirmation_id: str
    source_detail: str
    card_id: str = ""
    card_page: int | None = None
    has_frozen_cwd: bool = False
    frozen_cwd: str | None = None
    frozen_role: str = ""
    _seal: str = ""


@dataclass(frozen=True)
class HumanConfirmation:
    """Single-use host-verified human confirmation token with frozen render-time snapshot."""
    actor: str
    project_id: str
    role: str
    target_key: tuple[str, str, str]
    confirmation_id: str
    confirmed_at: str
    issuer_token: str = ""
    card_id: str = ""
    card_page: int | None = None
    has_frozen_cwd: bool = False
    frozen_cwd: str | None = None
    frozen_role: str = ""


@dataclass(frozen=True)
class ClaimedConfirmation:
    """Internal handle proving a HumanConfirmation was atomically claimed for ONE dispatch."""
    confirmation: HumanConfirmation
    _claim_seal: str = ""


@dataclass(frozen=True)
class DeliveryEvidence:
    leg: str
    target_key: tuple[str, str, str]
    evidence_type: str
    message_id: str
    delivered_at: str
    verified: bool = True
    dry_run: bool = False
    route_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["target_key"] = list(self.target_key)
        return d


@dataclass
class SlotResolution:
    exit_code: int
    action: str  # "deliver" | "need_pick" | "busy" | "need_evidence" | "error"
    project_id: str
    role: str
    slot_key: str
    expected_leg: str = ""
    expected_host_id: str = ""
    expected_host_role: str = ""
    candidate: Any = None
    candidates: list[Any] = field(default_factory=list)
    reason: str = ""
    message: str = ""
    slot_snapshot: dict[str, Any] | None = None
    retired_cleaned: list[str] = field(default_factory=list)
    expected_mutation: str = ""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_binding_gen() -> str:
    return "gen-" + uuid.uuid4().hex[:16]


def _nonempty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s if s else None


def normalize_leg(leg: str) -> str:
    if not isinstance(leg, str):
        return ""
    key = leg.strip().casefold()
    return LEG_ALIASES.get(key, key)


def normalize_host_role(reg_role: str) -> str:
    if not isinstance(reg_role, str):
        return ""
    key = reg_role.strip().casefold()
    return REGISTRY_TO_HOST_ROLE.get(key, key)


def candidate_key(candidate: Any) -> tuple[str, str, str]:
    c_leg = normalize_leg(getattr(candidate, "leg", ""))
    c_host = _nonempty_str(getattr(candidate, "hostId", "")) or ""
    c_id = _nonempty_str(getattr(candidate, "id", "")) or ""
    return (c_leg, c_host, c_id)


def make_slot_key(project_id: str, role: str) -> str:
    pid = _nonempty_str(project_id)
    rl = _nonempty_str(role)
    if not pid or not rl or ":" in pid or ":" in rl:
        raise SlotValidationError(
            "非法 project_id 或 role：%r / %r" % (project_id, role),
            exit_code=EXIT_ENV,
            reason="invalid_slot_key",
        )
    return "%s:%s" % (pid, rl)


def _mint_from_verified_host_event(
    event: VerifiedHostEvent,
    *,
    confirmed_at: str | None = None,
) -> HumanConfirmation:
    if not isinstance(event, VerifiedHostEvent) or event._seal != _HOST_GATE_SECRET:
        raise SlotPermissionError(
            "拒绝无宿主核验事件签发人工确认凭证（禁止普通调用方自报 actor 签发）",
            reason="unverified_host_event",
        )
    if event.actor not in ALLOWED_BOUND_BY:
        raise SlotPermissionError("非人工确认来源：%r" % (event.actor,), reason="origin_forbidden")
    if not _nonempty_str(event.confirmation_id):
        raise SlotPermissionError("宿主事件缺少 confirmation_id", reason="missing_confirmation_id")
    make_slot_key(event.project_id, event.role)
    if not all(event.target_key):
        raise SlotValidationError("候选缺少 leg/hostId/id 三元组", reason="target_incomplete")

    token = uuid.uuid4().hex
    conf = HumanConfirmation(
        actor=event.actor,
        project_id=event.project_id,
        role=event.role,
        target_key=event.target_key,
        confirmation_id=event.confirmation_id,
        confirmed_at=(confirmed_at or now_iso()),
        issuer_token=token,
        card_id=event.card_id,
        card_page=event.card_page,
        has_frozen_cwd=event.has_frozen_cwd,
        frozen_cwd=event.frozen_cwd,
        frozen_role=event.frozen_role,
    )
    with _CONFIRMATION_LOCK:
        _ISSUED_CONFIRMATIONS[token] = conf
    return conf


def _create_host_card_event(
    *,
    project_id: str,
    role: str,
    target_key: tuple[str, str, str],
    card_id: str,
    card_page: int,
    confirmation_id: str,
    has_frozen_cwd: bool,
    frozen_cwd: str | None,
    frozen_role: str,
) -> VerifiedHostEvent:
    """Internal helper invoked ONLY by card_renderer_v2 after verifying a host-signed receipt."""
    return VerifiedHostEvent(
        actor="human_card",
        project_id=project_id.strip(),
        role=role.strip(),
        target_key=target_key,
        confirmation_id=confirmation_id.strip(),
        source_detail="host_ui_card:%s@p%d" % (card_id, int(card_page)),
        card_id=card_id,
        card_page=int(card_page),
        has_frozen_cwd=has_frozen_cwd,
        frozen_cwd=frozen_cwd,
        frozen_role=frozen_role,
        _seal=_HOST_GATE_SECRET,
    )


def issue_confirmation_from_cli_tty(
    *,
    project_id: str,
    role: str,
    candidate: Any,
    confirmation_id: str | None = None,
    confirmed_at: str | None = None,
) -> HumanConfirmation:
    """Host interactive CLI confirmation gate using ONLY real process stdin TTY & live input.

    GPT v0.5 #1b: Production API accepts NO injectable isatty_fn or input_fn callbacks.
    Non-TTY processes are unconditionally rejected with `cli_not_interactive_tty`.
    """
    make_slot_key(project_id, role)
    tkey = candidate_key(candidate)
    if not all(tkey):
        raise SlotValidationError("候选缺少 leg/hostId/id 三元组", reason="target_incomplete")

    stdin_obj = sys.stdin
    is_tty = bool(stdin_obj is not None and hasattr(stdin_obj, "isatty") and stdin_obj.isatty())
    if not is_tty:
        raise SlotPermissionError(
            "非交互式 TTY 环境禁止签发 human_cli 确认凭证（须真实人工终端确认）",
            reason="cli_not_interactive_tty",
        )

    challenge = tkey[2][:8]
    prompt = "请确认绑定 %s:%s -> [%s/%s] %s（输入目标短 ID '%s' 确认，其余取消）：" % (
        project_id, role, tkey[0], tkey[1], tkey[2], challenge
    )
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = stdin_obj.readline()
    except (EOFError, OSError):
        raise SlotPermissionError("终端未收到人工确认输入", reason="cli_confirmation_cancelled")

    if not line or line.strip() != challenge:
        raise SlotPermissionError(
            "人工 CLI 确认口令不匹配（期望 %s），已取消绑定" % challenge,
            reason="cli_confirmation_cancelled",
        )

    raw_cwd = getattr(candidate, "cwd", None)
    has_cwd = isinstance(raw_cwd, str)
    frozen_cwd = raw_cwd if has_cwd else None
    frozen_role = (getattr(candidate, "role", "") or "").strip() if isinstance(getattr(candidate, "role", ""), str) else ""

    cid = _nonempty_str(confirmation_id) or ("cli-" + uuid.uuid4().hex[:12])
    event = VerifiedHostEvent(
        actor="human_cli",
        project_id=project_id.strip(),
        role=role.strip(),
        target_key=tkey,
        confirmation_id=cid,
        source_detail="host_cli_tty:%s" % challenge,
        has_frozen_cwd=has_cwd,
        frozen_cwd=frozen_cwd,
        frozen_role=frozen_role,
        _seal=_HOST_GATE_SECRET,
    )
    return _mint_from_verified_host_event(event, confirmed_at=confirmed_at)


def issue_human_confirmation(
    *,
    host_event: VerifiedHostEvent | None = None,
    actor: str = "",
    project_id: str = "",
    role: str = "",
    candidate: Any = None,
    confirmation_id: str = "",
    confirmed_at: str | None = None,
) -> HumanConfirmation:
    """Public confirmation minting gate — STRICTLY requires a sealed VerifiedHostEvent."""
    if host_event is None:
        raise SlotPermissionError(
            "禁止仅凭字符串自报 actor=%r 签发人工确认；必须经宿主专属发卡通道（HostCardChannel）"
            "或交互式人工 CLI 终端（issue_confirmation_from_cli_tty）核验后签发" % (actor,),
            reason="unverified_host_event",
        )
    return _mint_from_verified_host_event(host_event, confirmed_at=confirmed_at)


def _check_confirmation_common(
    confirmation: Any,
    *,
    project_id: str,
    role: str,
    expected_target_key: tuple[str, str, str],
) -> HumanConfirmation:
    if not isinstance(confirmation, HumanConfirmation):
        raise SlotPermissionError("缺少经宿主核验签发的 HumanConfirmation 凭证", reason="origin_forbidden")
    if confirmation.actor not in ALLOWED_BOUND_BY:
        raise SlotPermissionError("非人工确认来源：%r" % (confirmation.actor,), reason="origin_forbidden")
    if not _nonempty_str(confirmation.confirmation_id):
        raise SlotPermissionError("确认凭证缺少 confirmation_id", reason="missing_confirmation_id")
    tok = _nonempty_str(confirmation.issuer_token)
    if not tok or _ISSUED_CONFIRMATIONS.get(tok) != confirmation:
        raise SlotPermissionError(
            "确认凭证未经可信宿主 UI/CLI 门签发登记，拒绝自签绕过",
            reason="confirmation_unissued",
        )
    if confirmation.project_id != project_id or confirmation.role != role:
        raise SlotPermissionError(
            "确认凭证作用域不符：签发给 %s:%s，试图用于 %s:%s"
            % (confirmation.project_id, confirmation.role, project_id, role),
            reason="confirmation_scope_mismatch",
        )
    if confirmation.target_key != expected_target_key:
        raise SlotPermissionError(
            "确认凭证目标不符：签发给 %s，试图用于 %s"
            % (confirmation.target_key, expected_target_key),
            reason="confirmation_target_mismatch",
        )
    return confirmation


def validate_human_confirmation(
    confirmation: Any,
    *,
    project_id: str,
    role: str,
    expected_target_key: tuple[str, str, str],
    require_unused: bool = True,
) -> HumanConfirmation:
    """Validate confirmation authenticity, scope, and (by default) unused status."""
    with _CONFIRMATION_LOCK:
        conf = _check_confirmation_common(
            confirmation, project_id=project_id, role=role, expected_target_key=expected_target_key
        )
        if require_unused and conf.issuer_token in _CLAIMED_CONFIRMATION_TOKENS:
            raise SlotPermissionError(
                "确认凭证 %s 已被消费（每张凭证仅允许绑定一次派发，禁止复用）" % conf.confirmation_id,
                reason="confirmation_already_used",
            )
        return conf


def claim_human_confirmation(
    confirmation: Any,
    *,
    project_id: str,
    role: str,
    expected_target_key: tuple[str, str, str],
) -> ClaimedConfirmation:
    """Atomically claim a HumanConfirmation for ONE dispatch/bind cycle (GPT v0.5 #1c)."""
    with _CONFIRMATION_LOCK:
        conf = _check_confirmation_common(
            confirmation, project_id=project_id, role=role, expected_target_key=expected_target_key
        )
        if conf.issuer_token in _CLAIMED_CONFIRMATION_TOKENS:
            raise SlotPermissionError(
                "确认凭证 %s 已被消费，禁止二次派发或复用绑定" % conf.confirmation_id,
                reason="confirmation_already_used",
            )
        _CLAIMED_CONFIRMATION_TOKENS.add(conf.issuer_token)
        return ClaimedConfirmation(confirmation=conf, _claim_seal=_HOST_GATE_SECRET)


def validate_delivery_evidence(
    evidence: Any,
    *,
    expected_target_key: tuple[str, str, str],
) -> dict[str, Any]:
    if isinstance(evidence, DeliveryEvidence):
        ev = evidence.to_dict()
    elif isinstance(evidence, dict):
        ev = dict(evidence)
    else:
        raise SlotValidationError("缺少送达真证据（DeliveryEvidence）", reason="delivery_evidence_missing")

    if ev.get("dry_run") is not False:
        raise SlotValidationError("dry_run 结果不可作为送达证据", reason="evidence_dry_run")
    if ev.get("route_only") is not False:
        raise SlotValidationError("路线预览（route_only）不可作为送达证据", reason="evidence_route_only")
    if ev.get("verified") is not True:
        raise SlotValidationError("仅受理未核验（verified!=True）不可作为送达证据", reason="evidence_unverified")

    ev_type = _nonempty_str(ev.get("evidence_type"))
    if not ev_type or ev_type not in ALLOWED_EVIDENCE_TYPES:
        raise SlotValidationError("未知或不合格送达证据类型：%r" % (ev.get("evidence_type"),), reason="evidence_type_invalid")

    msg_id = _nonempty_str(ev.get("message_id"))
    if not msg_id:
        raise SlotValidationError("送达证据缺少合法字符串 message_id", reason="evidence_missing_msg_id")

    raw_tk = ev.get("target_key")
    if not isinstance(raw_tk, (list, tuple)) or len(raw_tk) != 3:
        raise SlotValidationError("送达证据缺少合法 target_key", reason="evidence_target_invalid")
    if not all(isinstance(part, str) and part.strip() for part in raw_tk):
        raise SlotValidationError("送达证据 target_key 元素须为非空字符串", reason="evidence_target_invalid")
    ev_tk = (normalize_leg(raw_tk[0]), raw_tk[1].strip(), raw_tk[2].strip())
    if ev_tk != expected_target_key:
        raise SlotValidationError(
            "送达证据目标不符：证据=%s，槽位=%s" % (ev_tk, expected_target_key),
            reason="evidence_target_mismatch",
        )

    ev_leg_raw = _nonempty_str(ev.get("leg"))
    if not ev_leg_raw:
        raise SlotValidationError("送达证据缺少合法字符串 leg", reason="evidence_leg_mismatch")
    ev_leg = normalize_leg(ev_leg_raw)
    if ev_leg != expected_target_key[0]:
        raise SlotValidationError("送达证据 leg 与目标不符", reason="evidence_leg_mismatch")

    expected_ev_type = LEG_TO_EVIDENCE_TYPE.get(ev_leg)
    if expected_ev_type and ev_type != expected_ev_type:
        raise SlotValidationError(
            "送达证据类型与腿不匹配：leg=%s 期望 %s，实得 %s" % (ev_leg, expected_ev_type, ev_type),
            reason="evidence_type_leg_mismatch",
        )

    deliv_at = _nonempty_str(ev.get("delivered_at")) or now_iso()
    return {
        "leg": ev_leg,
        "target_key": list(ev_tk),
        "evidence_type": ev_type,
        "message_id": msg_id,
        "delivered_at": deliv_at,
        "verified": True,
        "dry_run": False,
        "route_only": False,
    }


def _default_cwd_key(cwd: str) -> str:
    try:
        import codex_picker_adapter as cxa
        if hasattr(cxa, "fs_path_key"):
            return cxa.fs_path_key(cwd)
    except ImportError:
        pass
    p = str(cwd or "").strip()
    if p[:7].lower() == "file://":
        p = urllib.parse.unquote(p[7:])
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        elif p and p[0] not in "/\\" and (len(p) < 2 or p[1] != ":"):
            p = "//" + p
    if not p:
        return ""
    prefix = p[:8].replace("/", "\\")
    if prefix.lower() == "\\\\?\\unc\\":
        p = "\\\\" + p[8:]
    elif prefix.startswith("\\\\?\\"):
        p = p[4:]
    key = os.path.normcase(os.path.normpath(p))
    drive, tail = os.path.splitdrive(key)
    if drive.startswith("\\\\") and tail == "\\":
        return drive
    return key


def _verify_candidate(candidate: Any, allow_protected: bool = False) -> tuple[bool, str]:
    try:
        import window_picker as wp
        return wp.verify_target(candidate, allow_protected=allow_protected)
    except ImportError:
        reason = getattr(candidate, "unusableReason", "") or ""
        status = getattr(candidate, "status", "") or ""
        reachable = getattr(candidate, "reachable", True)
        if reason == "archived" or status == "archived":
            return False, "archived"
        if reason in ("cloud_no_rollout", "unloaded_disk_only", "no_local_record"):
            return False, reason
        if reason == "workspace_protected":
            return (True, "ok") if allow_protected else (False, "workspace_protected")
        if not reachable:
            return False, reason or "unreachable"
        return True, "ok"


def _candidate_matches_host_role(candidate: Any, expected_leg: str, expected_host_role: str) -> tuple[bool, str]:
    cand_role_raw = getattr(candidate, "role", "")
    cand_role = cand_role_raw.strip().casefold() if isinstance(cand_role_raw, str) else ""
    if cand_role in ("", "unknown"):
        return False, "identity_missing"
    if cand_role == "delegated":
        return False, "role_delegated"
    exp = normalize_host_role(expected_host_role)
    if exp in ("desktop", "feishu_bot", "peer"):
        if cand_role != exp:
            return False, "role_mismatch"
        return True, "ok"
    return False, "role_mismatch"


def inspect_busy_state(candidate: Any) -> tuple[str, str]:
    status_raw = getattr(candidate, "status", "")
    status = status_raw.strip().casefold() if isinstance(status_raw, str) else ""
    if status in BUSY_STATUSES:
        return "busy", status

    busy_ago = getattr(candidate, "busySecondsAgo", None)
    if busy_ago is not None:
        if isinstance(busy_ago, bool) or not isinstance(busy_ago, (int, float)):
            return "unknown", "invalid_busy_age"
        if not math.isfinite(busy_ago) or busy_ago < 0:
            return "unknown", "invalid_busy_age"
        if busy_ago < BUSY_WINDOW_SEC:
            return "busy", "busy_recent_%ds" % int(busy_ago)
        return "idle", "idle_age_ok"

    if status == "idle":
        return "idle", "status_idle"
    return "unknown", "busy_age_missing"


def _is_candidate_busy(candidate: Any) -> tuple[bool, str]:
    state, detail = inspect_busy_state(candidate)
    return (state == "busy"), (detail if state == "busy" else "")


def _read_json_strict(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise SlotConfigError("%s 缺失：%s" % (label, path), reason="file_missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SlotConfigError("%s 解析失败：%s（%s）" % (label, path, exc), reason="json_invalid")
    if not isinstance(data, dict):
        raise SlotConfigError("%s 顶层须为 JSON 对象：%s" % (label, path), reason="schema_invalid")
    return data


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        import safe_write as sw
        sw.write_text(str(path), text, mkdir=True)
        return
    except ImportError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    with open(tmp, "wb") as fh:
        fh.write(text.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    json.loads(tmp.read_text(encoding="utf-8"))
    os.replace(tmp, path)


def _slot_version_of(entry: Any) -> int:
    if not isinstance(entry, dict):
        return 0
    v = entry.get("slot_version")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        return v
    return 1  # Any pre-existing row without explicit version is at least version 1


def _validate_persisted_active_slot(
    slot: dict[str, Any],
    *,
    expected_project_id: str,
    expected_role: str,
    expected_leg: str,
    expected_host_id: str,
) -> tuple[bool, str]:
    row_pid = _nonempty_str(slot.get("project_id"))
    if row_pid != expected_project_id:
        return False, "slot_project_mismatch"
    row_role = _nonempty_str(slot.get("role"))
    if row_role != expected_role:
        return False, "slot_role_mismatch"

    bound_by = _nonempty_str(slot.get("bound_by"))
    if not bound_by or bound_by not in ALLOWED_BOUND_BY:
        return False, "untrusted_bound_by"

    conf_ref = _nonempty_str(slot.get("confirmation_ref"))
    if not conf_ref:
        return False, "missing_confirmation_ref"

    bgen = _nonempty_str(slot.get("binding_gen"))
    if not bgen:
        return False, "missing_binding_gen"

    tgt = slot.get("target")
    if not isinstance(tgt, dict):
        return False, "missing_target"
    t_leg_raw = _nonempty_str(tgt.get("leg"))
    t_host = _nonempty_str(tgt.get("hostId"))
    t_id = _nonempty_str(tgt.get("id"))
    if not t_leg_raw or not t_host or not t_id:
        return False, "incomplete_target"
    t_leg = normalize_leg(t_leg_raw)
    if t_leg != expected_leg or t_host != expected_host_id:
        return False, "platform_mismatch"
    tkey = (t_leg, t_host, t_id)

    cscope = slot.get("confirmation_scope")
    if not isinstance(cscope, dict):
        return False, "missing_confirmation_scope"
    if (
        _nonempty_str(cscope.get("project_id")) != expected_project_id
        or _nonempty_str(cscope.get("role")) != expected_role
        or _nonempty_str(cscope.get("confirmation_id")) != conf_ref
        or _nonempty_str(cscope.get("actor")) != bound_by
    ):
        return False, "confirmation_scope_mismatch"
    raw_stk = cscope.get("target_key")
    if not isinstance(raw_stk, list) or len(raw_stk) != 3 or not all(isinstance(x, str) and x.strip() for x in raw_stk):
        return False, "confirmation_scope_target_invalid"
    if (normalize_leg(raw_stk[0]), raw_stk[1].strip(), raw_stk[2].strip()) != tkey:
        return False, "confirmation_scope_target_mismatch"

    try:
        validate_delivery_evidence(slot.get("delivery_evidence"), expected_target_key=tkey)
    except SlotValidationError as exc:
        return False, exc.reason

    if t_leg == "codex":
        if not _nonempty_str(slot.get("cwd_snapshot")):
            return False, "codex_cwd_missing"

    return True, "ok"


class SlotStore:
    """Manages project x role slot bindings backed purely by hub paths."""

    def __init__(
        self,
        hub_root: Path | str | None = None,
        cli_root: str | None = None,
        clock: Callable[[], str] = now_iso,
    ):
        self._hub_root = Path(hub_root).resolve() if hub_root is not None else None
        self._cli_root = cli_root
        self._clock = clock

    def pin_root(self, *, for_write: bool) -> Path:
        if self._hub_root is not None:
            return self._hub_root
        try:
            import paths as hub_paths
            root, _src, _data = hub_paths.resolve_root(cli_root=self._cli_root, for_write=for_write)
            return Path(root).resolve()
        except Exception as exc:  # noqa: BLE001
            raise SlotConfigError("无法固定档案库根（for_write=%s）：%s" % (for_write, exc), reason="hub_root_error")

    def pinned(self, *, for_write: bool) -> "SlotStore":
        if self._hub_root is not None:
            return self
        root = self.pin_root(for_write=for_write)
        return SlotStore(hub_root=root, cli_root=self._cli_root, clock=self._clock)

    def resolve_hub_path(self, rel: str, *, for_write: bool = False) -> Path:
        if Path(rel).is_absolute() or rel.startswith(("/", "\\")) or ":" in rel:
            raise SlotConfigError("仅允许库内相对路径：%s" % rel, reason="invalid_rel_path")
        if self._hub_root is not None:
            target = (self._hub_root / rel.replace("/", os.sep)).resolve()
            if target != self._hub_root and self._hub_root not in target.parents:
                raise SlotConfigError("路径逃逸出库：%s" % rel, reason="path_escape")
            return target
        try:
            import paths as hub_paths
            return hub_paths.resolve(rel, cli_root=self._cli_root, for_write=for_write)
        except Exception as exc:  # noqa: BLE001
            raise SlotConfigError("无法解析档案库路径 %s：%s" % (rel, exc), reason="hub_root_error")

    def load_active_projects(self) -> dict[str, dict[str, Any]]:
        path = self.resolve_hub_path(ACTIVE_PROJECTS_REL, for_write=False)
        data = _read_json_strict(path, "active_projects.json")
        rows = data.get("active")
        if not isinstance(rows, list):
            raise SlotConfigError("active_projects.json 缺少合法 active 列表", reason="active_list_invalid")
        out: dict[str, dict[str, Any]] = {}
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                raise SlotConfigError("active_projects.json 第 %d 项非对象" % idx, reason="active_entry_invalid")
            pid = _nonempty_str(row.get("project_id"))
            parts = row.get("participants")
            if not pid or not isinstance(parts, list) or not all(isinstance(p, str) and p.strip() for p in parts):
                raise SlotConfigError(
                    "active_projects.json 第 %d 项缺合法 project_id/participants" % idx,
                    reason="active_entry_invalid",
                )
            if pid in out:
                raise SlotConfigError("active_projects.json 存在重复 project_id：%s" % pid, reason="duplicate_project")
            out[pid] = row
        return out

    def load_platform_registry(self) -> dict[str, dict[str, Any]]:
        path = self.resolve_hub_path(PLATFORM_REGISTRY_REL, for_write=False)
        data = _read_json_strict(path, "平台注册表.json")
        plats = data.get("platforms")
        if not isinstance(plats, dict):
            raise SlotConfigError("平台注册表.json 缺少合法 platforms 对象", reason="registry_invalid")
        out: dict[str, dict[str, Any]] = {}
        for role_name, decl in plats.items():
            if not isinstance(decl, dict):
                continue
            leg_raw = _nonempty_str(decl.get("leg"))
            host_id = _nonempty_str(decl.get("hostId"))
            if not leg_raw or not host_id:
                raise SlotConfigError(
                    "平台注册表.json 角色 %s 缺少 leg/hostId" % role_name,
                    reason="registry_entry_invalid",
                )
            copy = dict(decl)
            copy["leg_raw"] = leg_raw
            copy["leg"] = normalize_leg(leg_raw)
            copy["hostId"] = host_id
            copy["host_role"] = normalize_host_role(decl.get("role") or "")
            out[role_name] = copy
        return out

    def _empty_ledger(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._clock(),
            "slots": {},
        }

    def load_slots_doc(self) -> dict[str, Any]:
        path = self.resolve_hub_path(PROJECT_SLOTS_REL, for_write=False)
        if not path.exists():
            return self._empty_ledger()
        data = _read_json_strict(path, "project_slots.json")
        if not isinstance(data.get("slots"), dict):
            raise SlotConfigError("project_slots.json 缺少合法 slots 对象", reason="slots_schema_invalid")
        return data

    def _check_disk_cas(
        self,
        path: Path,
        cas: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Final on-disk CAS check distinguishing absent row vs any existing row (active/stale/retired)."""
        slot_key = cas["slot_key"]
        if path.exists():
            live_doc = _read_json_strict(path, "project_slots.json")
            live_slots = live_doc.get("slots") if isinstance(live_doc.get("slots"), dict) else {}
            live_entry = live_slots.get(slot_key)
        else:
            live_entry = None

        mode = cas.get("mode") or "active_gen"
        if mode == "absent_only":
            # GPT v0.5 #3: row MUST NOT exist at all (even stale/retired means a newer commit happened!)
            if live_entry is None:
                return None
            raise SlotConcurrencyError(
                "槽位 %s 原无记录但磁盘已出现新记录（status=%s, ver=%s），拒绝覆盖"
                % (slot_key, live_entry.get("status") if isinstance(live_entry, dict) else "?", _slot_version_of(live_entry)),
                current_entry=live_entry if isinstance(live_entry, dict) else None,
            )

        if mode == "exact_version":
            exp_ver = cas["expected_version"]
            exp_gen = cas.get("expected_gen")
            exp_status = cas.get("expected_status")
            if not isinstance(live_entry, dict):
                raise SlotConcurrencyError("槽位 %s 预期版本 %d 但磁盘记录缺失" % (slot_key, exp_ver), current_entry=None)
            cur_ver = _slot_version_of(live_entry)
            if cur_ver != exp_ver:
                raise SlotConcurrencyError(
                    "槽位 %s 版本已变更（期望 %d，实得 %d），拒绝覆盖" % (slot_key, exp_ver, cur_ver),
                    current_entry=live_entry,
                )
            if exp_gen is not None and _nonempty_str(live_entry.get("binding_gen")) != exp_gen:
                raise SlotConcurrencyError("槽位 %s 代际已变更，拒绝覆盖" % slot_key, current_entry=live_entry)
            if exp_status is not None and live_entry.get("status") != exp_status:
                raise SlotConcurrencyError("槽位 %s 状态已变更，拒绝覆盖" % slot_key, current_entry=live_entry)
            return live_entry

        # Default mode: active_gen (must be active + same project/role/gen/target_key)
        if not isinstance(live_entry, dict) or not self._entry_matches_generation(
            live_entry,
            expected_project_id=cas["project_id"],
            expected_role=cas["role"],
            expected_gen=cas["expected_gen"],
            expected_target_key=cas.get("expected_target_key"),
        ):
            raise SlotConcurrencyError(
                "槽位 %s 在 compare-to-write 间隙发生并发变更，拒绝覆盖" % slot_key,
                current_entry=live_entry if isinstance(live_entry, dict) else None,
            )
        return live_entry

    def save_slots_doc(
        self,
        doc: dict[str, Any],
        *,
        cas: dict[str, Any] | None = None,
    ) -> Path:
        if not isinstance(doc, dict) or not isinstance(doc.get("slots"), dict):
            raise SlotConfigError("拒绝写入非法 slots 文档", reason="slots_schema_invalid")
        path = self.resolve_hub_path(PROJECT_SLOTS_REL, for_write=True)
        with _exclusive_ledger_lock(path):
            if cas is not None:
                self._check_disk_cas(path, cas)
            payload = {
                "schema_version": doc.get("schema_version") or SCHEMA_VERSION,
                "updated_at": self._clock(),
                "slots": doc["slots"],
            }
            _write_json_atomic(path, payload)
        return path

    def _validate_project_role(
        self,
        project_id: str,
        role: str,
        *,
        allow_paused: bool = False,
        projects: dict[str, dict[str, Any]] | None = None,
        platforms: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        make_slot_key(project_id, role)
        proj_map = projects if projects is not None else self.load_active_projects()
        plat_map = platforms if platforms is not None else self.load_platform_registry()

        proj = proj_map.get(project_id)
        if proj is None:
            raise SlotValidationError(
                "未找到活跃项目：%s（请检查 00-config/active_projects.json）" % project_id,
                exit_code=EXIT_NO_TARGET,
                reason="unknown_project",
            )
        participants = [p.strip() for p in proj.get("participants", [])]
        if role not in participants:
            raise SlotValidationError(
                "角色 %s 不在项目 %s 的参与名单中（当前：%s）" % (role, project_id, ", ".join(participants)),
                exit_code=EXIT_UNQUALIFIED,
                reason="role_not_participant",
            )
        status = (proj.get("status") or "active").strip().casefold()
        if status == "paused" and not allow_paused:
            raise SlotValidationError(
                "项目 %s 当前处于 paused 状态（如需继续请显式加 --allow-paused）" % project_id,
                exit_code=EXIT_UNQUALIFIED,
                reason="project_paused",
            )
        plat = plat_map.get(role)
        if plat is None:
            raise SlotValidationError(
                "角色 %s 未在 00-config/平台注册表.json 中登记" % role,
                exit_code=EXIT_ENV,
                reason="unknown_role_platform",
            )
        return proj, plat

    def reconcile_retired_slots(
        self,
        *,
        active_projects: dict[str, dict[str, Any]] | None = None,
        mutate: bool = True,
    ) -> list[str]:
        store = self.pinned(for_write=mutate)
        proj_map = active_projects if active_projects is not None else store.load_active_projects()
        slots_path = store.resolve_hub_path(PROJECT_SLOTS_REL, for_write=mutate)
        if not mutate:
            doc = store.load_slots_doc()
            changed = []
            for key, entry in doc["slots"].items():
                if not isinstance(entry, dict):
                    continue
                pid = _nonempty_str(entry.get("project_id")) or (key.split(":", 1)[0] if ":" in key else "")
                if pid and pid not in proj_map and entry.get("status") != "retired":
                    changed.append(key)
            return changed

        with _exclusive_ledger_lock(slots_path):
            doc = store.load_slots_doc()
            slots = doc["slots"]
            changed = []
            stamp = store._clock()
            for key, entry in slots.items():
                if not isinstance(entry, dict):
                    continue
                pid = _nonempty_str(entry.get("project_id")) or (key.split(":", 1)[0] if ":" in key else "")
                if pid and pid not in proj_map and entry.get("status") != "retired":
                    entry["status"] = "retired"
                    entry["retired_at"] = stamp
                    entry["retired_reason"] = "project_not_active"
                    entry["slot_version"] = _slot_version_of(entry) + 1
                    changed.append(key)
            if changed:
                store.save_slots_doc(doc)
            return changed

    def retire_project(self, project_id: str, *, reason: str = "explicit_retire") -> list[str]:
        pid = _nonempty_str(project_id)
        if not pid:
            raise SlotValidationError("project_id 不能为空", exit_code=EXIT_ENV, reason="invalid_project_id")
        store = self.pinned(for_write=True)
        slots_path = store.resolve_hub_path(PROJECT_SLOTS_REL, for_write=True)
        with _exclusive_ledger_lock(slots_path):
            doc = store.load_slots_doc()
            slots = doc["slots"]
            changed: list[str] = []
            stamp = store._clock()
            for key, entry in slots.items():
                if not isinstance(entry, dict):
                    continue
                entry_pid = _nonempty_str(entry.get("project_id")) or (key.split(":", 1)[0] if ":" in key else "")
                if entry_pid == pid and entry.get("status") != "retired":
                    entry["status"] = "retired"
                    entry["retired_at"] = stamp
                    entry["retired_reason"] = reason
                    entry["slot_version"] = _slot_version_of(entry) + 1
                    changed.append(key)
            if changed:
                store.save_slots_doc(doc)
            return changed

    def _push_previous_target(self, entry: dict[str, Any], old_target: dict[str, Any], reason: str, stamp: str) -> None:
        prev = entry.get("previous_targets")
        if not isinstance(prev, list):
            prev = []
        if old_target and all(_nonempty_str(old_target.get(k)) for k in ("leg", "hostId", "id")):
            record = {
                "leg": normalize_leg(old_target["leg"]),
                "hostId": old_target["hostId"].strip(),
                "id": old_target["id"].strip(),
                "title_snapshot": entry.get("title_snapshot") or "",
                "replaced_at": stamp,
                "reason": reason,
            }
            prev = [
                p for p in prev
                if not (
                    isinstance(p, dict)
                    and normalize_leg(p.get("leg", "")) == record["leg"]
                    and p.get("hostId") == record["hostId"]
                    and p.get("id") == record["id"]
                )
            ]
            prev.insert(0, record)
            prev = prev[:MAX_PREVIOUS_TARGETS]
        entry["previous_targets"] = prev

    def _entry_matches_generation(
        self,
        entry: dict[str, Any],
        *,
        expected_project_id: str,
        expected_role: str,
        expected_gen: str | None,
        expected_target_key: tuple[str, str, str] | None,
    ) -> bool:
        if entry.get("status") != "active":
            return False
        if _nonempty_str(entry.get("project_id")) != expected_project_id:
            return False
        if _nonempty_str(entry.get("role")) != expected_role:
            return False
        if expected_gen is not None and _nonempty_str(entry.get("binding_gen")) != expected_gen:
            return False
        if expected_target_key is not None:
            tgt = entry.get("target") or {}
            cur_tk = (
                normalize_leg(tgt.get("leg", "")),
                _nonempty_str(tgt.get("hostId")) or "",
                _nonempty_str(tgt.get("id")) or "",
            )
            if cur_tk != expected_target_key:
                return False
        return True

    def bind_slot(
        self,
        project_id: str,
        role: str,
        candidate: Any,
        *,
        confirmation: HumanConfirmation | ClaimedConfirmation,
        delivery_evidence: DeliveryEvidence | dict[str, Any] | None = None,
        allow_paused: bool = False,
        delivered: bool = False,
        expected_prior_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically bind/rebind (project_id, role) under exclusive lock + exact-state CAS.

        - Single-use confirmation enforcement: if a raw HumanConfirmation is passed directly,
          atomically claims it here; if a ClaimedConfirmation (already claimed for this dispatch)
          is passed, verifies its private claim seal.
        - `expected_prior_state` distinguishes:
          * `{"exists": False}` -> ONLY succeeds if slot row does NOT exist on disk at all
            (any existing row — active, stale, or retired — triggers SlotConcurrencyError!).
          * `{"exists": True, "slot_version": N, "binding_gen": G, "status": S}` -> ONLY succeeds
            if the live disk row matches that exact version, generation, and status.
        """
        store = self.pinned(for_write=True)
        tkey = candidate_key(candidate)
        if not all(tkey):
            raise SlotValidationError("候选缺少 leg/hostId/id 三元组", reason="target_incomplete")

        if isinstance(confirmation, ClaimedConfirmation):
            if confirmation._claim_seal != _HOST_GATE_SECRET:
                raise SlotPermissionError("伪造的 ClaimedConfirmation", reason="origin_forbidden")
            conf = validate_human_confirmation(
                confirmation.confirmation,
                project_id=project_id,
                role=role,
                expected_target_key=tkey,
                require_unused=False,
            )
        else:
            claimed = claim_human_confirmation(
                confirmation,
                project_id=project_id,
                role=role,
                expected_target_key=tkey,
            )
            conf = claimed.confirmation

        if not delivered:
            raise SlotValidationError(
                "未完成真实投递前禁止写入 active 槽位（必须 delivered=True 且附送达证据）",
                reason="delivery_required",
            )
        ev_dict = validate_delivery_evidence(delivery_evidence, expected_target_key=tkey)

        _proj, plat = store._validate_project_role(project_id, role, allow_paused=allow_paused)
        cand_leg, cand_host, cand_id = tkey
        if cand_leg != plat["leg"] or cand_host != plat["hostId"]:
            raise SlotValidationError(
                "目标平台 (%s/%s) 与角色 %s 注册平台 (%s/%s) 不符"
                % (cand_leg, cand_host, role, plat["leg"], plat["hostId"]),
                reason="platform_mismatch",
            )
        role_ok, role_reason = _candidate_matches_host_role(candidate, plat["leg"], plat["host_role"])
        if not role_ok:
            raise SlotValidationError(
                "目标真实角色 (%r) 与角色 %s 期望宿主角色 (%s) 不符：%s"
                % (getattr(candidate, "role", ""), role, plat["host_role"], role_reason),
                reason=role_reason,
            )
        ok, v_reason = _verify_candidate(candidate, allow_protected=False)
        if not ok:
            raise SlotValidationError(
                "候选未通过 verify_target 安全校验：%s" % v_reason,
                reason="verify_failed:%s" % v_reason,
            )

        # Prefer immutable render-time frozen_cwd from confirmation (GPT v0.5 #2b)
        if conf.has_frozen_cwd:
            cand_cwd = conf.frozen_cwd if isinstance(conf.frozen_cwd, str) else ""
        else:
            raw_cwd = getattr(candidate, "cwd", None)
            cand_cwd = raw_cwd.strip() if isinstance(raw_cwd, str) else ""
        if cand_leg == "codex" and not cand_cwd:
            raise SlotValidationError("Codex 活绑定必须保留确认时的有效 cwd", reason="codex_cwd_required")

        key = make_slot_key(project_id, role)
        slots_path = store.resolve_hub_path(PROJECT_SLOTS_REL, for_write=True)
        with _exclusive_ledger_lock(slots_path):
            doc = store.load_slots_doc()
            existing = doc["slots"].get(key)

            cas_spec: dict[str, Any] | None = None
            if expected_prior_state is not None:
                if not expected_prior_state.get("exists"):
                    if existing is not None:
                        raise SlotConcurrencyError(
                            "槽位 %s 原无记录但已被并发写入（status=%s），拒绝覆盖"
                            % (key, existing.get("status") if isinstance(existing, dict) else "?"),
                            current_entry=existing if isinstance(existing, dict) else None,
                        )
                    cas_spec = {"slot_key": key, "mode": "absent_only"}
                else:
                    exp_ver = int(expected_prior_state["slot_version"])
                    exp_gen = expected_prior_state.get("binding_gen")
                    exp_status = expected_prior_state.get("status")
                    if (
                        not isinstance(existing, dict)
                        or _slot_version_of(existing) != exp_ver
                        or (exp_gen is not None and _nonempty_str(existing.get("binding_gen")) != exp_gen)
                        or (exp_status is not None and existing.get("status") != exp_status)
                    ):
                        raise SlotConcurrencyError(
                            "槽位 %s 原状态/版本已变更，拒绝覆盖较新记录" % key,
                            current_entry=existing if isinstance(existing, dict) else None,
                        )
                    cas_spec = {
                        "slot_key": key,
                        "mode": "exact_version",
                        "expected_version": exp_ver,
                        "expected_gen": exp_gen,
                        "expected_status": exp_status,
                    }

            stamp = store._clock()
            new_target = {"leg": cand_leg, "hostId": cand_host, "id": cand_id}
            next_version = _slot_version_of(existing) + 1 if isinstance(existing, dict) else 1
            entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}

            old_target = entry.get("target")
            if isinstance(old_target, dict) and (
                normalize_leg(old_target.get("leg", "")) != cand_leg
                or old_target.get("hostId") != cand_host
                or old_target.get("id") != cand_id
            ):
                store._push_previous_target(entry, old_target, reason="rebound_by_%s" % conf.actor, stamp=stamp)
            else:
                entry.setdefault("previous_targets", [])

            title_snap = (
                getattr(candidate, "display_title", "")
                or getattr(candidate, "title", "")
                or getattr(candidate, "alias", "")
                or cand_id[:8]
            )
            entry.update({
                "project_id": project_id,
                "role": role,
                "target": new_target,
                "slot_version": next_version,
                "binding_gen": new_binding_gen(),
                "host_role_snapshot": (conf.frozen_role or (getattr(candidate, "role", "") or "").strip()),
                "title_snapshot": title_snap,
                "cwd_snapshot": cand_cwd,
                "bound_at": stamp,
                "bound_by": conf.actor,
                "confirmation_ref": conf.confirmation_id,
                "confirmation_scope": {
                    "actor": conf.actor,
                    "project_id": conf.project_id,
                    "role": conf.role,
                    "target_key": list(conf.target_key),
                    "confirmation_id": conf.confirmation_id,
                    "confirmed_at": conf.confirmed_at,
                    "card_id": conf.card_id,
                    "card_page": conf.card_page,
                },
                "delivery_evidence": ev_dict,
                "last_verified_ok_at": stamp,
                "last_delivered_at": ev_dict["delivered_at"],
                "status": "active",
                "stale_reason": "",
                "retired_at": "",
                "retired_reason": "",
            })
            doc["slots"][key] = entry
            store.save_slots_doc(doc, cas=cas_spec)
            return entry

    def mark_slot_stale(
        self,
        project_id: str,
        role: str,
        reason: str,
        *,
        expected_gen: str | None = None,
        expected_target_key: tuple[str, str, str] | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Atomic lock + on-disk CAS stale transition (increments slot_version)."""
        store = self.pinned(for_write=True)
        key = make_slot_key(project_id, role)
        slots_path = store.resolve_hub_path(PROJECT_SLOTS_REL, for_write=True)
        with _exclusive_ledger_lock(slots_path):
            doc = store.load_slots_doc()
            entry = doc["slots"].get(key)
            if not isinstance(entry, dict):
                return False, None
            cas_spec: dict[str, Any] | None = None
            if expected_gen is not None or expected_target_key is not None:
                if not store._entry_matches_generation(
                    entry,
                    expected_project_id=project_id,
                    expected_role=role,
                    expected_gen=expected_gen,
                    expected_target_key=expected_target_key,
                ):
                    return False, entry
                cas_spec = {
                    "slot_key": key,
                    "mode": "active_gen",
                    "project_id": project_id,
                    "role": role,
                    "expected_gen": expected_gen,
                    "expected_target_key": expected_target_key,
                }

            stamp = store._clock()
            old_target = entry.get("target")
            if isinstance(old_target, dict):
                store._push_previous_target(entry, old_target, reason="stale:%s" % reason, stamp=stamp)
            entry["status"] = "stale"
            entry["stale_reason"] = reason
            entry["slot_version"] = _slot_version_of(entry) + 1
            doc["slots"][key] = entry
            try:
                store.save_slots_doc(doc, cas=cas_spec)
            except SlotConcurrencyError as exc:
                return False, exc.current_entry
            return True, entry

    def touch_slot_delivery(
        self,
        project_id: str,
        role: str,
        *,
        delivery_evidence: DeliveryEvidence | dict[str, Any],
        expected_gen: str | None = None,
        expected_target_key: tuple[str, str, str] | None = None,
        title_snapshot: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        store = self.pinned(for_write=True)
        key = make_slot_key(project_id, role)
        slots_path = store.resolve_hub_path(PROJECT_SLOTS_REL, for_write=True)
        with _exclusive_ledger_lock(slots_path):
            doc = store.load_slots_doc()
            entry = doc["slots"].get(key)
            if not isinstance(entry, dict):
                return False, None
            if not store._entry_matches_generation(
                entry,
                expected_project_id=project_id,
                expected_role=role,
                expected_gen=expected_gen,
                expected_target_key=expected_target_key,
            ):
                return False, entry

            tgt = entry.get("target") or {}
            tkey = (
                normalize_leg(tgt.get("leg", "")),
                _nonempty_str(tgt.get("hostId")) or "",
                _nonempty_str(tgt.get("id")) or "",
            )
            ev_dict = validate_delivery_evidence(delivery_evidence, expected_target_key=tkey)
            stamp = store._clock()
            entry["last_verified_ok_at"] = stamp
            entry["last_delivered_at"] = ev_dict["delivered_at"]
            entry["delivery_evidence"] = ev_dict
            if title_snapshot:
                entry["title_snapshot"] = title_snapshot
            doc["slots"][key] = entry
            cas_spec = {
                "slot_key": key,
                "mode": "active_gen",
                "project_id": project_id,
                "role": role,
                "expected_gen": expected_gen if expected_gen is not None else entry.get("binding_gen"),
                "expected_target_key": expected_target_key or tkey,
            }
            try:
                store.save_slots_doc(doc, cas=cas_spec)
            except SlotConcurrencyError as exc:
                return False, exc.current_entry
            return True, entry

    def slots_for_target(
        self,
        leg: str,
        host_id: str,
        target_id: str,
        *,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        n_leg = normalize_leg(leg)
        doc = self.load_slots_doc()
        hits: list[dict[str, Any]] = []
        for entry in doc["slots"].values():
            if not isinstance(entry, dict):
                continue
            if active_only and entry.get("status") != "active":
                continue
            tgt = entry.get("target") or {}
            if (
                normalize_leg(tgt.get("leg", "")) == n_leg
                and tgt.get("hostId") == host_id
                and tgt.get("id") == target_id
            ):
                hits.append(entry)
        return hits

    def _filter_role_candidates(
        self,
        candidates: list[Any],
        expected_leg: str,
        expected_host_id: str,
        expected_host_role: str,
    ) -> list[Any]:
        seen = set()
        out = []
        for c in candidates:
            c_leg, c_host, c_id = candidate_key(c)
            if c_leg != expected_leg or c_host != expected_host_id or not c_id:
                continue
            role_ok, _rr = _candidate_matches_host_role(c, expected_leg, expected_host_role)
            if not role_ok:
                continue
            key = (c_leg, c_host, c_id)
            if key in seen:
                continue
            ok, _reason = _verify_candidate(c, allow_protected=False)
            if not ok:
                continue
            seen.add(key)
            out.append(c)
        return out

    def _leg_is_authoritative(
        self,
        expected_leg: str,
        expected_host_id: str,
        expected_host_role: str,
        candidates: list[Any],
        leg_status: dict[str, dict[str, Any]] | None,
    ) -> tuple[bool, str]:
        if not isinstance(leg_status, dict):
            return False, "leg_status_missing"

        info = leg_status.get(expected_leg)
        if info is None:
            for k, v in leg_status.items():
                if normalize_leg(k) == expected_leg:
                    info = v
                    break
        if not isinstance(info, dict):
            return False, "leg_not_queried"
        if info.get("disabled") is True or info.get("stub") is True:
            return False, "leg_disabled_or_stub"
        if info.get("ok") is not True:
            return False, "leg_error:%s" % (str(info.get("error") or "unhealthy").strip())
        if "error" not in info or info["error"] != "":
            return False, "leg_error_missing_or_nonempty"
        if info.get("truncated") is not False:
            return False, "leg_truncated_or_missing"

        covered_hosts = info.get("hosts") if "hosts" in info else info.get("covered_hosts")
        if not isinstance(covered_hosts, (list, tuple, set)):
            return False, "host_coverage_missing"
        if expected_host_id not in covered_hosts:
            return False, "host_not_covered"

        covered_roles = info.get("roles") if "roles" in info else info.get("covered_roles")
        if not isinstance(covered_roles, (list, tuple, set)):
            return False, "role_coverage_missing"
        norm_roles = {normalize_host_role(str(r)) for r in covered_roles}
        if normalize_host_role(expected_host_role) not in norm_roles:
            return False, "role_not_covered"

        for c in candidates:
            if normalize_leg(getattr(c, "leg", "")) == expected_leg and getattr(c, "truncated", False):
                return False, "leg_truncated"
        return True, "ok"

    def resolve_slot_route(
        self,
        project_id: str,
        role: str,
        candidates: list[Any],
        *,
        rebind: bool = False,
        allow_paused: bool = False,
        reconcile: bool = True,
        mutate: bool = False,
        leg_status: dict[str, dict[str, Any]] | None = None,
        cwd_normalizer: Callable[[str], str] | None = None,
    ) -> SlotResolution:
        slot_key = make_slot_key(project_id, role)
        try:
            store = self.pinned(for_write=mutate)
            projects = store.load_active_projects()
            platforms = store.load_platform_registry()
        except SlotError as exc:
            return SlotResolution(
                exit_code=exc.exit_code,
                action="error",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                reason=exc.reason,
                message=str(exc),
            )

        retired_cleaned: list[str] = []
        try:
            if reconcile:
                retired_cleaned = store.reconcile_retired_slots(active_projects=projects, mutate=mutate)
            _proj, plat = store._validate_project_role(
                project_id,
                role,
                allow_paused=allow_paused,
                projects=projects,
                platforms=platforms,
            )
            doc = store.load_slots_doc()
        except SlotError as exc:
            return SlotResolution(
                exit_code=exc.exit_code,
                action="error",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                reason=exc.reason,
                message=str(exc),
                retired_cleaned=retired_cleaned,
            )

        expected_leg = plat["leg"]
        expected_host = plat["hostId"]
        expected_host_role = plat["host_role"]
        role_candidates = store._filter_role_candidates(
            candidates, expected_leg, expected_host, expected_host_role
        )
        slot = doc["slots"].get(slot_key)

        if rebind:
            return SlotResolution(
                exit_code=EXIT_AMBIGUOUS,
                action="need_pick",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                expected_leg=expected_leg,
                expected_host_id=expected_host,
                expected_host_role=expected_host_role,
                candidates=role_candidates,
                reason="rebind_requested",
                message="已请求主动换棒（%s），请从 %s 活跃列表中确认新窗" % (slot_key, role),
                slot_snapshot=slot,
                retired_cleaned=retired_cleaned,
            )

        if not isinstance(slot, dict) or slot.get("status") != "active":
            cur_status = slot.get("status") if isinstance(slot, dict) else "unbound"
            stale_note = ("（上次失效原因：%s）" % slot.get("stale_reason")) if (isinstance(slot, dict) and slot.get("stale_reason")) else ""
            return SlotResolution(
                exit_code=EXIT_AMBIGUOUS,
                action="need_pick",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                expected_leg=expected_leg,
                expected_host_id=expected_host,
                expected_host_role=expected_host_role,
                candidates=role_candidates,
                reason="no_active_slot:%s" % cur_status,
                message="槽位 %s 当前无已确认 active 绑定（状态：%s）%s，请选窗确认" % (slot_key, cur_status, stale_note),
                slot_snapshot=slot if isinstance(slot, dict) else None,
                retired_cleaned=retired_cleaned,
            )

        ev_ok, ev_reason = _validate_persisted_active_slot(
            slot,
            expected_project_id=project_id,
            expected_role=role,
            expected_leg=expected_leg,
            expected_host_id=expected_host,
        )
        if not ev_ok:
            return SlotResolution(
                exit_code=EXIT_AMBIGUOUS,
                action="need_pick",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                expected_leg=expected_leg,
                expected_host_id=expected_host,
                expected_host_role=expected_host_role,
                candidates=role_candidates,
                reason="invalid_slot_evidence:%s" % ev_reason,
                message="槽位 %s 账本证据或作用域校验未通过（%s），已拒绝直投，请重新选窗确认" % (slot_key, ev_reason),
                slot_snapshot=slot,
                retired_cleaned=retired_cleaned,
            )

        tgt = slot["target"]
        t_leg = normalize_leg(tgt["leg"])
        t_host = tgt["hostId"].strip()
        t_id = tgt["id"].strip()
        tkey = (t_leg, t_host, t_id)
        cur_gen = slot["binding_gen"]

        def _degrade_stale(stale_code: str, human_msg: str) -> SlotResolution:
            if mutate:
                _applied, updated = store.mark_slot_stale(
                    project_id, role, stale_code, expected_gen=cur_gen, expected_target_key=tkey
                )
                exp_mut = ""
            else:
                updated = slot
                exp_mut = "would_mark_stale:%s" % stale_code
            return SlotResolution(
                exit_code=EXIT_AMBIGUOUS,
                action="need_pick",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                expected_leg=expected_leg,
                expected_host_id=expected_host,
                expected_host_role=expected_host_role,
                candidates=role_candidates,
                reason="stale:%s" % stale_code,
                message=human_msg,
                slot_snapshot=updated,
                retired_cleaned=retired_cleaned,
                expected_mutation=exp_mut,
            )

        matched = None
        for c in candidates:
            if candidate_key(c) == tkey:
                matched = c
                break

        if matched is None:
            auth_ok, auth_reason = store._leg_is_authoritative(
                expected_leg, expected_host, expected_host_role, candidates, leg_status
            )
            if not auth_ok:
                return SlotResolution(
                    exit_code=EXIT_ENV,
                    action="need_evidence",
                    project_id=project_id,
                    role=role,
                    slot_key=slot_key,
                    expected_leg=expected_leg,
                    expected_host_id=expected_host,
                    expected_host_role=expected_host_role,
                    candidates=role_candidates,
                    reason="need_evidence:%s" % auth_reason,
                    message="腿 %s 快照覆盖不全或异常（%s），无法权威判定原绑定窗缺失，暂不动原绑定" % (expected_leg, auth_reason),
                    slot_snapshot=slot,
                    retired_cleaned=retired_cleaned,
                )
            return _degrade_stale(
                "missing",
                "原绑定窗「%s」已不在活跃快照中，请选接棒新窗" % (slot.get("title_snapshot") or t_id[:8]),
            )

        ok, v_reason = _verify_candidate(matched, allow_protected=False)
        if not ok:
            if v_reason in TRANSIENT_SOURCE_REASONS or (
                v_reason not in AUTHORITATIVE_STALE_REASONS
                and getattr(matched, "truncated", False)
            ):
                return SlotResolution(
                    exit_code=EXIT_ENV,
                    action="need_evidence",
                    project_id=project_id,
                    role=role,
                    slot_key=slot_key,
                    expected_leg=expected_leg,
                    expected_host_id=expected_host,
                    expected_host_role=expected_host_role,
                    candidates=role_candidates,
                    reason="need_evidence:%s" % v_reason,
                    message="原绑定窗「%s」当前来源不可用（%s），非权威失效，暂不动原绑定" % (slot.get("title_snapshot") or t_id[:8], v_reason),
                    slot_snapshot=slot,
                    retired_cleaned=retired_cleaned,
                )
            if v_reason in AUTHORITATIVE_STALE_REASONS:
                return _degrade_stale(
                    v_reason,
                    "原绑定窗「%s」已失效（%s），请选接棒新窗" % (slot.get("title_snapshot") or t_id[:8], v_reason),
                )
            return SlotResolution(
                exit_code=EXIT_ENV,
                action="need_evidence",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                expected_leg=expected_leg,
                expected_host_id=expected_host,
                expected_host_role=expected_host_role,
                candidates=role_candidates,
                reason="need_evidence:%s" % v_reason,
                message="原绑定窗「%s」状态暂不可核实（%s），暂不动原绑定" % (slot.get("title_snapshot") or t_id[:8], v_reason),
                slot_snapshot=slot,
                retired_cleaned=retired_cleaned,
            )

        role_ok, role_reason = _candidate_matches_host_role(matched, expected_leg, expected_host_role)
        if not role_ok:
            if role_reason == "identity_missing":
                return SlotResolution(
                    exit_code=EXIT_ENV,
                    action="need_evidence",
                    project_id=project_id,
                    role=role,
                    slot_key=slot_key,
                    expected_leg=expected_leg,
                    expected_host_id=expected_host,
                    expected_host_role=expected_host_role,
                    candidates=role_candidates,
                    reason="need_evidence:identity_missing",
                    message="原绑定窗「%s」宿主角色未知（unknown），非权威冲突，暂不动原绑定" % (slot.get("title_snapshot") or t_id[:8]),
                    slot_snapshot=slot,
                    retired_cleaned=retired_cleaned,
                )
            return _degrade_stale(
                role_reason,
                "原绑定窗「%s」真实角色与注册表不符（%s），请重新选窗确认" % (slot.get("title_snapshot") or t_id[:8], role_reason),
            )

        norm_fn = cwd_normalizer or _default_cwd_key
        has_saved_cwd = "cwd_snapshot" in slot and isinstance(slot["cwd_snapshot"], str)
        saved_cwd = slot["cwd_snapshot"] if has_saved_cwd else None
        raw_cur_cwd = getattr(matched, "cwd", None)
        cur_cwd = raw_cur_cwd if isinstance(raw_cur_cwd, str) else ""
        if (t_leg == "codex" and not _nonempty_str(cur_cwd)) or (
            saved_cwd is not None and norm_fn(saved_cwd) != norm_fn(cur_cwd)
        ):
            return _degrade_stale(
                "cwd_drift",
                "原绑定窗「%s」工作区发生漂移或缺失，请重新选窗确认" % (slot.get("title_snapshot") or t_id[:8]),
            )

        busy_state, busy_detail = inspect_busy_state(matched)
        if busy_state == "busy":
            return SlotResolution(
                exit_code=EXIT_BUSY,
                action="busy",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                expected_leg=expected_leg,
                expected_host_id=expected_host,
                expected_host_role=expected_host_role,
                candidate=matched,
                reason="busy:%s" % busy_detail,
                message="槽位 %s 绑定窗「%s」正忙（%s），等待空闲重试，绝不漂移改投" % (
                    slot_key,
                    getattr(matched, "title", "") or t_id[:8],
                    busy_detail,
                ),
                slot_snapshot=slot,
                retired_cleaned=retired_cleaned,
            )

        if busy_state == "unknown":
            return SlotResolution(
                exit_code=EXIT_ENV,
                action="need_evidence",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                expected_leg=expected_leg,
                expected_host_id=expected_host,
                expected_host_role=expected_host_role,
                candidate=matched,
                reason="busy_state_unknown:%s" % busy_detail,
                message="槽位 %s 绑定窗「%s」忙态证据未知（%s），阻止静默直投，请先取得实时证据" % (
                    slot_key,
                    getattr(matched, "title", "") or t_id[:8],
                    busy_detail,
                ),
                slot_snapshot=slot,
                retired_cleaned=retired_cleaned,
            )

        return SlotResolution(
            exit_code=EXIT_OK,
            action="deliver",
            project_id=project_id,
            role=role,
            slot_key=slot_key,
            expected_leg=expected_leg,
            expected_host_id=expected_host,
            expected_host_role=expected_host_role,
            candidate=matched,
            reason="active_slot_verified",
            message="命中已确认活槽位 %s -> [%s] %s (%s)" % (
                slot_key,
                expected_leg,
                getattr(matched, "display_title", "") or getattr(matched, "title", "") or t_id[:8],
                t_id[:8],
            ),
            slot_snapshot=slot,
            retired_cleaned=retired_cleaned,
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="项目×角色槽位账本管理（默认只读）")
    ap.add_argument("--root", help="显式档案库根（可选）")
    ap.add_argument("--list", action="store_true", help="列出当前所有槽位")
    ap.add_argument("--reconcile", action="store_true", help="执行一次读时懒回收巡检")
    ap.add_argument("--retire-project", help="显式将指定项目所有槽位标为 retired")
    args = ap.parse_args(argv)

    store = SlotStore(cli_root=args.root)
    try:
        if args.retire_project:
            changed = store.retire_project(args.retire_project)
            print(json.dumps({"ok": True, "retired": changed}, ensure_ascii=False))
            return EXIT_OK
        if args.reconcile:
            changed = store.reconcile_retired_slots(mutate=True)
            print(json.dumps({"ok": True, "reconciled": changed}, ensure_ascii=False))
            return EXIT_OK
        doc = store.load_slots_doc()
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return EXIT_OK
    except SlotError as exc:
        print("SLOT_ERROR [%s]: %s" % (exc.reason, exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
