# -*- coding: utf-8 -*-
"""picker_slots_orchestrator.py — Layer 1 orchestration prototype for picker.py (v0.5).

Hardened against GPT round-3 final review & desktop_bot production bridge contract:
1. Strict target_key check on authoritative exit 6 rejections (#2):
   `_payload_target_matches()` REQUIRES `target_key` to be present and equal to the exact
   in-flight target triple; missing `target_key` returns `need_evidence` and NEVER marks stale.
2. Concurrent confirm protection (#3):
   `confirm_and_deliver_slot()` captures the pre-send slot generation (`prior_gen`, or `None`
   when unbound/non-active) and commits `bind_slot(..., expected_prior_gen=prior_gen)` under
   exclusive lock + on-disk CAS. If a newer confirmation B committed before older A's delivery
   returned, B stays active and A returns `bound=False, action="delivered_to_superseded_confirm",
   do_not_retry=True`.
3. Explicit empty cwd preservation aligned with desktop_bot's None sentinel (#4):
   - `deliver_by_slot()`: missing `cwd_snapshot` -> `expected_cwd=None`; explicitly saved `""`
     -> `expected_cwd=""` (NEVER overwritten with candidate's new cwd!); non-empty -> that value.
   - `confirm_and_deliver_slot()`: preserves candidate's explicit `""` vs `None`.
4. Defensive busy guard in both deliver and confirm paths:
   `code == EXIT_BUSY or (rej_reason == "busy_window" and code != EXIT_OK)`.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any, Callable

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

import project_slots as ps


def _validate_registry_entries_fallback(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ps.SlotConfigError("Codex 会话登记表损坏不可解析：%s（%s）" % (path, exc), reason="codex_registry_invalid")
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        raise ps.SlotConfigError("Codex 会话登记表结构非法（缺少 lines 列表）：%s" % path, reason="codex_registry_invalid")
    for idx, row in enumerate(data["lines"]):
        if not isinstance(row, dict):
            raise ps.SlotConfigError("Codex 会话登记表第 %d 项非对象" % idx, reason="codex_registry_invalid")
        if not ps._nonempty_str(row.get("thread_id")):
            raise ps.SlotConfigError("Codex 会话登记表第 %d 项缺少 thread_id" % idx, reason="codex_registry_invalid")
        if row.get("leg", "codex") != "codex":
            raise ps.SlotConfigError("Codex 会话登记表第 %d 项 leg 非 codex" % idx, reason="codex_registry_invalid")
        for fld in ("alias", "legacy_alias", "别名", "display_title", "用途", "kind", "hostId"):
            if row.get(fld) is not None and not isinstance(row.get(fld), str):
                raise ps.SlotConfigError("Codex 会话登记表第 %d 项字段 %s 非字符串" % (idx, fld), reason="codex_registry_invalid")


def resolve_codex_registry_path(store: ps.SlotStore) -> Path:
    plats = store.load_platform_registry()
    gpt = plats.get("gpt") or {}
    rel = (gpt.get("session_registry") or "").strip()
    if not rel:
        raise ps.SlotConfigError(
            "00-config/平台注册表.json 中 gpt 缺少 session_registry 字段",
            reason="codex_registry_undeclared",
        )
    path = store.resolve_hub_path(rel, for_write=False)
    if not path.is_file():
        raise ps.SlotConfigError(
            "Codex 会话登记表不存在于新库：%s" % path,
            reason="codex_registry_missing",
        )
    try:
        import codex_picker_adapter as cxa
        if hasattr(cxa, "_register"):
            try:
                cxa._register(path)
                return path
            except (OSError, ValueError) as exc:
                raise ps.SlotConfigError(
                    "Codex 会话登记表未通过适配器严格校验：%s（%s）" % (path, exc),
                    reason="codex_registry_invalid",
                )
    except ImportError:
        pass
    _validate_registry_entries_fallback(path)
    return path


def _invoke_gather(
    gather_fn: Callable[..., Any],
    *,
    registry_path: Path | None,
) -> tuple[list[Any], dict[str, dict[str, Any]] | None]:
    sig = inspect.signature(gather_fn)
    params = sig.parameters
    accepts_reg = (
        "registry_path" in params
        or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    )
    if accepts_reg:
        raw = gather_fn(registry_path=registry_path)
    else:
        if registry_path is not None:
            raise ps.SlotConfigError(
                "枚举入口不接受 pinned registry_path 参数，拒绝退回默认登记册",
                reason="gather_missing_registry_param",
            )
        raw = gather_fn()

    if isinstance(raw, dict) and "candidates" in raw:
        cands = list(raw.get("candidates") or [])
        legs = raw.get("legs") if isinstance(raw.get("legs"), dict) else None
        return cands, legs
    if isinstance(raw, list):
        return raw, None
    raise ValueError("gather_fn returned unexpected type: %s" % type(raw).__name__)


def _extract_delivery_evidence(
    payload: dict[str, Any],
    *,
    expected_target_key: tuple[str, str, str],
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("dry_run") is True or payload.get("route_only") is True:
        return None
    if "route" in payload and not payload.get("delivery_evidence"):
        return None
    ev = payload.get("delivery_evidence")
    if ev is None:
        return None
    return ps.validate_delivery_evidence(ev, expected_target_key=expected_target_key)


def _payload_target_matches(payload: dict[str, Any], expected_target_key: tuple[str, str, str]) -> bool:
    """GPT round-3 #2: Require explicit target_key on authoritative exit 6 rejections (missing -> False)."""
    raw = payload.get("target_key")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return False
    if not all(isinstance(x, str) and x.strip() for x in raw):
        return False
    return (ps.normalize_leg(raw[0]), raw[1].strip(), raw[2].strip()) == expected_target_key


def _is_busy_response(code: int, rej_reason: str) -> bool:
    return code == ps.EXIT_BUSY or (rej_reason == "busy_window" and code != ps.EXIT_OK)


def _prepare_role_registry(store: ps.SlotStore, project_id: str, role: str, *, allow_paused: bool) -> tuple[dict[str, Any], Path | None]:
    _proj, plat = store._validate_project_role(project_id, role, allow_paused=allow_paused)
    reg_path: Path | None = None
    if plat["leg"] == "codex":
        reg_path = resolve_codex_registry_path(store)
    return plat, reg_path


def pick_by_slot(
    store: ps.SlotStore,
    project_id: str,
    role: str,
    gather_fn: Callable[..., Any],
    *,
    rebind: bool = False,
    allow_paused: bool = False,
    reconcile: bool = True,
    mutate: bool = False,
    cwd_normalizer: Callable[[str], str] | None = None,
) -> tuple[ps.SlotResolution, Path | None]:
    slot_key = "%s:%s" % (project_id, role)
    try:
        pinned_store = store.pinned(for_write=mutate)
        _plat, reg_path = _prepare_role_registry(pinned_store, project_id, role, allow_paused=allow_paused)
    except ps.SlotError as exc:
        return (
            ps.SlotResolution(
                exit_code=exc.exit_code,
                action="error",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                reason=exc.reason,
                message=str(exc),
            ),
            None,
        )

    try:
        candidates, leg_status = _invoke_gather(gather_fn, registry_path=reg_path)
    except ps.SlotError as exc:
        return (
            ps.SlotResolution(
                exit_code=exc.exit_code,
                action="error",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                reason=exc.reason,
                message=str(exc),
            ),
            reg_path,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            ps.SlotResolution(
                exit_code=ps.EXIT_ENV,
                action="need_evidence",
                project_id=project_id,
                role=role,
                slot_key=slot_key,
                reason="gather_failed:%s" % type(exc).__name__,
                message="拉取活跃窗口快照失败：%s" % exc,
            ),
            reg_path,
        )

    res = pinned_store.resolve_slot_route(
        project_id,
        role,
        candidates,
        rebind=rebind,
        allow_paused=allow_paused,
        reconcile=reconcile,
        mutate=mutate,
        leg_status=leg_status,
        cwd_normalizer=cwd_normalizer,
    )
    return res, reg_path


def deliver_by_slot(
    store: ps.SlotStore,
    project_id: str,
    role: str,
    text: str,
    gather_fn: Callable[..., Any],
    dispatch_fn: Callable[..., tuple[int, dict[str, Any]]],
    *,
    rebind: bool = False,
    allow_paused: bool = False,
    dry_run: bool = True,
    cwd_normalizer: Callable[[str], str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Resolve active slot -> keyword dispatch -> atomic CAS stale/delivery handling."""
    mutate = not dry_run
    try:
        pinned_store = store.pinned(for_write=mutate)
    except ps.SlotError as exc:
        return exc.exit_code, {"ok": False, "action": "error", "reason": exc.reason, "message": str(exc)}

    res, reg_path = pick_by_slot(
        pinned_store,
        project_id,
        role,
        gather_fn,
        rebind=rebind,
        allow_paused=allow_paused,
        mutate=mutate,
        cwd_normalizer=cwd_normalizer,
    )
    if res.action != "deliver" or res.candidate is None:
        return res.exit_code, {
            "ok": False,
            "action": res.action,
            "slot_key": res.slot_key,
            "reason": res.reason,
            "message": res.message,
            "candidates_count": len(res.candidates),
            "retired_cleaned": res.retired_cleaned,
            "expected_mutation": res.expected_mutation,
        }

    snap = res.slot_snapshot or {}
    flight_gen = snap.get("binding_gen")
    flight_tkey = ps.candidate_key(res.candidate)

    # GPT round-3 #4 & desktop_bot None-sentinel contract:
    # - saved "" -> pass "" verbatim (never fall back to candidate's new cwd!)
    # - missing/None cwd_snapshot -> pass None sentinel
    if "cwd_snapshot" in snap and isinstance(snap["cwd_snapshot"], str):
        expected_cwd: str | None = snap["cwd_snapshot"]
    else:
        expected_cwd = None

    code, payload = dispatch_fn(
        candidate=res.candidate,
        text=text,
        dry_run=dry_run,
        expected_cwd=expected_cwd,
        registry_path=reg_path,
    )
    payload = payload if isinstance(payload, dict) else {}
    rej_reason = (payload.get("reason") or "").strip() if isinstance(payload.get("reason"), str) else ""

    # 1. Busy returned at send time (defensive guard: never misclassify exit 0)
    if _is_busy_response(code, rej_reason):
        return ps.EXIT_BUSY, {
            "ok": False,
            "action": "busy",
            "slot_key": res.slot_key,
            "target_key": list(flight_tkey),
            "reason": rej_reason or "busy_window",
            "message": "目标窗口投前忙态，保留当前槽位等待空闲重试",
            "dispatch": payload,
        }

    # 2. Authoritative target rejection (EXIT_UNQUALIFIED=6) -> requires explicit matching target_key
    if code == ps.EXIT_UNQUALIFIED and rej_reason in ps.AUTHORITATIVE_STALE_REASONS:
        if not _payload_target_matches(payload, flight_tkey):
            return ps.EXIT_ENV, {
                "ok": False,
                "action": "need_evidence",
                "slot_key": res.slot_key,
                "reason": "rejection_target_mismatch",
                "message": "拒绝回执缺少或目标键与本次在途目标不符，不动绑定",
                "dispatch": payload,
            }
        exp_mut = ""
        rebound_during_flight = False
        current_active_target = None
        if mutate:
            applied, cur_entry = pinned_store.mark_slot_stale(
                project_id,
                role,
                rej_reason,
                expected_gen=flight_gen,
                expected_target_key=flight_tkey,
            )
            if not applied:
                rebound_during_flight = True
                current_active_target = (cur_entry or {}).get("target")
        else:
            exp_mut = "would_mark_stale:%s" % rej_reason

        if rebound_during_flight:
            return ps.EXIT_UNQUALIFIED, {
                "ok": False,
                "action": "slot_rebound",
                "slot_key": res.slot_key,
                "slot_rebound_during_flight": True,
                "in_flight_target": list(flight_tkey),
                "current_active_target": current_active_target,
                "reason": "stale_ignored_slot_rebound:%s" % rej_reason,
                "message": "旧目标在途被权威拒绝（%s），但槽位已改绑新窗，已保留新绑定未覆盖" % rej_reason,
                "dispatch": payload,
            }
        return ps.EXIT_AMBIGUOUS, {
            "ok": False,
            "action": "need_pick",
            "slot_key": res.slot_key,
            "reason": "stale:%s" % rej_reason,
            "message": "发送前权威复验拒绝原绑定窗（%s），请人工重新确认目标" % rej_reason,
            "expected_mutation": exp_mut,
            "dispatch": payload,
        }

    # 3. Transient / unverified / landed-without-receipt -> need_evidence, NEVER mutate active slot
    if code != ps.EXIT_OK or not payload.get("ok"):
        is_transient = (
            code in (ps.EXIT_ENV, ps.EXIT_VERIFY_FAIL)
            or rej_reason in ps.TRANSIENT_SOURCE_REASONS
            or rej_reason.startswith(("bridge_exit_", "reverify_error:", "commit_error:"))
        )
        landed = bool(payload.get("landed"))
        msg = (
            "消息可能已落入目标但缺少可核验回执（%s），切勿盲目重投，暂不动原绑定" % (rej_reason or "unverified")
            if landed
            else "发送未确认或遇瞬时故障（%s），暂不动原绑定" % (rej_reason or "dispatch_failed")
        )
        return code, {
            "ok": False,
            "action": "need_evidence" if is_transient else "error",
            "slot_key": res.slot_key,
            "target_key": list(flight_tkey),
            "landed": landed,
            "do_not_retry": landed,
            "reason": rej_reason or "dispatch_failed",
            "message": msg,
            "dispatch": payload,
        }

    if dry_run:
        return ps.EXIT_OK, {
            "ok": True,
            "action": "deliver",
            "slot_key": res.slot_key,
            "dry_run": True,
            "delivered_recorded": False,
            "target_id": flight_tkey[2],
            "dispatch": payload,
        }

    # 4. Live mode: require strict verifiable delivery evidence before recording delivery
    try:
        ev = _extract_delivery_evidence(payload, expected_target_key=flight_tkey)
    except ps.SlotValidationError as exc:
        return ps.EXIT_VERIFY_FAIL, {
            "ok": False,
            "action": "need_evidence",
            "slot_key": res.slot_key,
            "delivered_recorded": False,
            "reason": exc.reason,
            "message": str(exc),
            "dispatch": payload,
        }
    if ev is None:
        return ps.EXIT_VERIFY_FAIL, {
            "ok": False,
            "action": "need_evidence",
            "slot_key": res.slot_key,
            "delivered_recorded": False,
            "reason": "delivery_evidence_missing",
            "message": "发送入口未返回可核验送达证据（拒绝把路线预览/仅受理记为已送达）",
            "dispatch": payload,
        }

    title_snap = (
        getattr(res.candidate, "display_title", "")
        or getattr(res.candidate, "title", "")
        or getattr(res.candidate, "alias", "")
    )
    applied, cur_entry = pinned_store.touch_slot_delivery(
        project_id,
        role,
        delivery_evidence=ev,
        expected_gen=flight_gen,
        expected_target_key=flight_tkey,
        title_snapshot=title_snap,
    )
    if not applied:
        return ps.EXIT_OK, {
            "ok": True,
            "action": "delivered_to_prior_binding",
            "slot_key": res.slot_key,
            "dry_run": False,
            "delivered_recorded": False,
            "slot_rebound_during_flight": True,
            "do_not_retry": True,
            "delivered_target": list(flight_tkey),
            "current_active_target": (cur_entry or {}).get("target"),
            "message": "消息已送达旧绑定窗，但在途期间槽位已改绑新窗；已保留新绑定并禁止重复投递",
            "dispatch": payload,
        }
    return ps.EXIT_OK, {
        "ok": True,
        "action": "deliver",
        "slot_key": res.slot_key,
        "dry_run": False,
        "delivered_recorded": True,
        "target_id": flight_tkey[2],
        "dispatch": payload,
        "retired_cleaned": res.retired_cleaned,
    }


def confirm_and_deliver_slot(
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
) -> tuple[int, dict[str, Any]]:
    """After host-verified human confirmation: claim single-use token -> verify frozen cwd
    -> capture exact prior slot state -> deliver -> CAS bind_slot.

    GPT v0.5 fixes:
    - #1c: atomically claims `confirmation` BEFORE dispatching (one confirmation = one dispatch;
      a second attempt returns EXIT_UNQUALIFIED / confirmation_already_used with 0 posts).
    - #2b: verifies live candidate cwd against render-time `confirmation.frozen_cwd` (post-render
      cwd mutation -> EXIT_UNQUALIFIED / cwd_drift with 0 posts) and forwards frozen_cwd to bridge.
    - #3: captures exact prior_state distinguishing `{"exists": False}` from ANY existing row
      (active / stale / retired) via monotonic `slot_version`.
    """
    mutate = not dry_run
    tkey = ps.candidate_key(selected_candidate)
    if not all(tkey):
        return ps.EXIT_UNQUALIFIED, {"ok": False, "bound": False, "reason": "target_incomplete"}

    # Validate confirmation authenticity & unused status BEFORE side effects
    try:
        conf = ps.validate_human_confirmation(
            confirmation, project_id=project_id, role=role, expected_target_key=tkey, require_unused=True
        )
    except ps.SlotError as exc:
        return exc.exit_code, {"ok": False, "bound": False, "reason": exc.reason, "message": str(exc)}

    try:
        pinned_store = store.pinned(for_write=mutate)
        plat, reg_path = _prepare_role_registry(pinned_store, project_id, role, allow_paused=allow_paused)
        c_leg, c_host, _c_id = tkey
        if c_leg != plat["leg"] or c_host != plat["hostId"]:
            return ps.EXIT_UNQUALIFIED, {
                "ok": False,
                "bound": False,
                "reason": "platform_mismatch",
                "message": "所选窗口平台与角色不符",
            }
        role_ok, role_reason = ps._candidate_matches_host_role(selected_candidate, plat["leg"], plat["host_role"])
        if not role_ok:
            return ps.EXIT_UNQUALIFIED, {
                "ok": False,
                "bound": False,
                "reason": role_reason,
                "message": "所选窗口宿主角色与注册表不符：%s" % role_reason,
            }
        ok, v_reason = ps._verify_candidate(selected_candidate, allow_protected=False)
        if not ok:
            return ps.EXIT_UNQUALIFIED, {
                "ok": False,
                "bound": False,
                "reason": "verify_failed:%s" % v_reason,
                "message": "所选窗口复验不合格：%s" % v_reason,
            }

        # GPT v0.5 #2b: verify live candidate cwd against render-time frozen cwd snapshot
        raw_sel_cwd = getattr(selected_candidate, "cwd", None)
        if conf.has_frozen_cwd:
            frozen_cwd = conf.frozen_cwd if isinstance(conf.frozen_cwd, str) else ""
            live_cwd = raw_sel_cwd if isinstance(raw_sel_cwd, str) else ""
            if ps._default_cwd_key(frozen_cwd) != ps._default_cwd_key(live_cwd):
                return ps.EXIT_UNQUALIFIED, {
                    "ok": False,
                    "bound": False,
                    "reason": "cwd_drift",
                    "message": "发卡后候选工作区已漂移（封存=%r，当前=%r），拒绝派发" % (frozen_cwd, live_cwd),
                }
            expected_cwd: str | None = frozen_cwd
        else:
            expected_cwd = raw_sel_cwd if isinstance(raw_sel_cwd, str) else None

        if c_leg == "codex" and not ps._nonempty_str(expected_cwd):
            return ps.EXIT_UNQUALIFIED, {
                "ok": False,
                "bound": False,
                "reason": "codex_cwd_required",
                "message": "Codex 活绑定缺少有效工作区 cwd",
            }

        # GPT v0.5 #3: capture exact prior slot state distinguishing absent vs any existing row
        slot_key = ps.make_slot_key(project_id, role)
        pre_doc = pinned_store.load_slots_doc()
        pre_entry = pre_doc["slots"].get(slot_key)
        if pre_entry is None:
            prior_state: dict[str, Any] = {"exists": False}
        elif isinstance(pre_entry, dict):
            prior_state = {
                "exists": True,
                "slot_version": ps._slot_version_of(pre_entry),
                "binding_gen": ps._nonempty_str(pre_entry.get("binding_gen")),
                "status": pre_entry.get("status"),
            }
        else:
            prior_state = {"exists": True, "slot_version": 1, "binding_gen": None, "status": None}
    except ps.SlotError as exc:
        return exc.exit_code, {"ok": False, "bound": False, "reason": exc.reason, "message": str(exc)}

    # GPT v0.5 #1c: atomically claim single-use confirmation right before dispatch (only on live send)
    claimed_conf: ps.ClaimedConfirmation | None = None
    if not dry_run:
        try:
            claimed_conf = ps.claim_human_confirmation(
                conf, project_id=project_id, role=role, expected_target_key=tkey
            )
        except ps.SlotError as exc:
            return exc.exit_code, {"ok": False, "bound": False, "reason": exc.reason, "message": str(exc)}

    code, payload = dispatch_fn(
        candidate=selected_candidate,
        text=text,
        dry_run=dry_run,
        expected_cwd=expected_cwd,
        registry_path=reg_path,
    )
    payload = payload if isinstance(payload, dict) else {}
    rej_reason = (payload.get("reason") or "").strip() if isinstance(payload.get("reason"), str) else ""

    if _is_busy_response(code, rej_reason):
        return ps.EXIT_BUSY, {
            "ok": False,
            "bound": False,
            "action": "busy",
            "reason": rej_reason or "busy_window",
            "dispatch": payload,
        }
    if code != ps.EXIT_OK or not payload.get("ok"):
        landed = bool(payload.get("landed"))
        is_transient = (
            code in (ps.EXIT_ENV, ps.EXIT_VERIFY_FAIL)
            or rej_reason in ps.TRANSIENT_SOURCE_REASONS
            or rej_reason.startswith(("bridge_exit_", "reverify_error:", "commit_error:"))
        )
        return code, {
            "ok": False,
            "bound": False,
            "action": "need_evidence" if is_transient else "error",
            "landed": landed,
            "do_not_retry": landed,
            "reason": rej_reason or "delivery_failed",
            "dispatch": payload,
        }
    if dry_run:
        return ps.EXIT_OK, {
            "ok": True,
            "dry_run": True,
            "bound": False,
            "note": "dry_run 验证通过，未写入 _state/project_slots.json",
            "dispatch": payload,
        }

    try:
        ev = _extract_delivery_evidence(payload, expected_target_key=tkey)
    except ps.SlotValidationError as exc:
        return ps.EXIT_VERIFY_FAIL, {
            "ok": False,
            "bound": False,
            "action": "need_evidence",
            "reason": exc.reason,
            "message": str(exc),
            "dispatch": payload,
        }
    if ev is None:
        return ps.EXIT_VERIFY_FAIL, {
            "ok": False,
            "bound": False,
            "action": "need_evidence",
            "reason": "delivery_evidence_missing",
            "message": "发送入口未返回可核验送达证据（路线预览/干跑/仅受理不可写入 active 槽位）",
            "dispatch": payload,
        }

    assert claimed_conf is not None
    try:
        entry = pinned_store.bind_slot(
            project_id,
            role,
            selected_candidate,
            confirmation=claimed_conf,
            delivery_evidence=ev,
            allow_paused=allow_paused,
            delivered=True,
            expected_prior_state=prior_state,
        )
    except ps.SlotConcurrencyError as exc:
        return ps.EXIT_OK, {
            "ok": True,
            "dry_run": False,
            "bound": False,
            "action": "delivered_to_superseded_confirm",
            "slot_rebound_during_flight": True,
            "do_not_retry": True,
            "delivered_target": list(tkey),
            "current_active_target": (exc.current_entry or {}).get("target"),
            "message": "旧确认消息已送达，但发送期间已有更新的槽位记录提交；已保留较新记录及其状态并禁止重复投递",
            "dispatch": payload,
        }

    return ps.EXIT_OK, {
        "ok": True,
        "dry_run": False,
        "bound": True,
        "slot": entry,
        "dispatch": payload,
    }
