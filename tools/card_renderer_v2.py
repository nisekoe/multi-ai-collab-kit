# -*- coding: utf-8 -*-
"""card_renderer_v2.py — Path A/B selection card with host-only issuance channel & frozen snapshots (v0.6).

Hardened against GPT v0.5 final review #1a & #2:
1. Public `build_ranked_card_grouped()` is PURE RENDERING — it NEVER registers a binding-eligible
   card into `_ISSUED_CARDS`. Ordinary callers cannot self-issue cards or submit caller-forged
   answers to mint `human_card` confirmations.
2. Host-exclusive issuance & UI receipt verification (`HostCardChannel`):
   - Only a host-owned `HostCardChannel(ask_user_fn)` (bound to the real UI ask_user_question
     callback and holding a private HMAC key) can issue a binding-eligible card AND produce a
     host-authenticated `HostCardReceipt` signed over `(card_id, page, receipt_id, answers)`.
   - `confirm_from_issued_card()` STRICTLY requires a valid `HostCardReceipt` matching the
     originating `HostCardChannel`'s HMAC signature, exact `card_id`, exact `page`, and an
     unconsumed issued record. Plain caller-supplied answer lists are unconditionally rejected
     (`invalid_host_receipt`).
3. Immutable render-time option snapshot freezing (#2b):
   - When `HostCardChannel` issues a card, every visible option's `(target_key, cwd, role, title)`
     is frozen into an immutable `FrozenOptionSnapshot` BEFORE rendering to the UI.
   - Mutating `candidate.cwd` after card render CANNOT alter the frozen `cwd` carried in the
     minted `HumanConfirmation` (`has_frozen_cwd`, `frozen_cwd`) — downstream dispatch uses the
     frozen snapshot and detects post-render drift.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

import picker_ranker as pr
import project_slots as ps

REC_PREFIX = "★ [推荐] "
REC_ESCAPE_PREFIX = "〔原题〕"
REC_GROUP_ID = "grp_recommended"
REC_HEADER = "★ 推荐目标"
PAGE_SIZE = 8
NAV_NEXT = "下一页 →"
NAV_CANCEL = "取消／不投"
NAV_KEYWORD = "输入关键字缩小"
RESERVED_LABELS = frozenset({NAV_NEXT, NAV_CANCEL, NAV_KEYWORD})


@dataclass(frozen=True)
class FrozenOptionSnapshot:
    """Immutable snapshot of a candidate captured at card issuance time (immune to post-render mutation)."""
    target_key: tuple[str, str, str]
    has_cwd: bool
    cwd_snapshot: str | None
    role_snapshot: str
    title_snapshot: str
    question_id: str
    page: int
    label: str


@dataclass(frozen=True)
class HostCardReceipt:
    """Host-authenticated UI receipt proving answers came from a real ask_user_question interaction."""
    card_id: str
    page: int
    receipt_id: str
    answers: tuple[dict[str, Any], ...]
    channel_id: str
    mac: str


# Host-issued card ledger & single process-wide host bootstrap authority
_ISSUED_CARDS: dict[str, dict[str, Any]] = {}
_AUTHORITY_SEAL = uuid.uuid4().hex
_ACTIVE_HOST_CHANNEL: "HostCardChannel | None" = None


def _receipt_mac(secret_key: bytes, card_id: str, page: int, receipt_id: str, answers: Any) -> str:
    canonical = json.dumps(
        {"card_id": card_id, "page": int(page), "receipt_id": receipt_id, "answers": answers},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(secret_key, canonical, hashlib.sha256).hexdigest()


def initialize_host_card_authority(
    ask_user_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> "HostCardChannel":
    """Single-shot host bootstrap entry point binding the process's real ask_user_question UI.

    Once initialized by the host runtime, ordinary callers CANNOT construct a second
    HostCardChannel or re-initialize a fake callback without holding the active channel's
    private `close()` handle.
    """
    global _ACTIVE_HOST_CHANNEL
    if _ACTIVE_HOST_CHANNEL is not None:
        raise ps.SlotPermissionError(
            "宿主发卡权威已由真实宿主初始化锁定，禁止普通调用方二次注册伪造 UI 通道",
            reason="host_authority_already_initialized",
        )
    ch = HostCardChannel(ask_user_fn, _seal=_AUTHORITY_SEAL)
    _ACTIVE_HOST_CHANNEL = ch
    return ch


class HostCardChannel:
    """Host-exclusive UI channel binding real ask_user_question display to card issuance & receipts.

    Cannot be instantiated directly by ordinary callers; must be obtained via the single-shot
    host bootstrap `initialize_host_card_authority(real_ask_user_fn)`.
    """

    def __init__(
        self,
        ask_user_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        *,
        _seal: str = "",
    ):
        if _seal != _AUTHORITY_SEAL:
            raise ps.SlotPermissionError(
                "禁止普通调用方直接构造 HostCardChannel；须由宿主引导 initialize_host_card_authority()",
                reason="host_authority_required",
            )
        if not callable(ask_user_fn):
            raise TypeError("ask_user_fn must be callable")
        self._ask_user_fn = ask_user_fn
        self._channel_id = "hch-" + uuid.uuid4().hex[:12]
        self._secret = uuid.uuid4().bytes + uuid.uuid4().bytes
        self._closed = False

    def close(self) -> None:
        """Release host authority (only callable by the holder of this channel instance)."""
        global _ACTIVE_HOST_CHANNEL
        self._closed = True
        if _ACTIVE_HOST_CHANNEL is self:
            _ACTIVE_HOST_CHANNEL = None

    @property
    def channel_id(self) -> str:
        return self._channel_id

    def _register_card(self, card: dict[str, Any], *, project_id: str, role: str) -> dict[str, Any]:
        pid = ps._nonempty_str(project_id)
        rl = ps._nonempty_str(role)
        if not pid or not rl:
            raise ps.SlotValidationError("宿主发卡必须指定合法 project_id 与 role", reason="invalid_slot_key")

        cur_page = int(card.get("page") or 0)
        card_id = card["card_id"]
        frozen_by_opt: dict[tuple[str, int, str], FrozenOptionSnapshot] = {}
        frozen_by_label: dict[str, dict[tuple[str, str, str], FrozenOptionSnapshot]] = {}
        orig_candidates: dict[tuple[str, str, str], Any] = {}

        for (qid, p, lbl), cand in (card.get("option_map") or {}).items():
            if int(p) != cur_page:
                continue
            tkey = ps.candidate_key(cand)
            raw_cwd = getattr(cand, "cwd", None)
            has_cwd = isinstance(raw_cwd, str)
            cwd_snap = raw_cwd if has_cwd else None
            raw_role = getattr(cand, "role", "")
            role_snap = raw_role.strip() if isinstance(raw_role, str) else ""
            title_snap = (
                getattr(cand, "display_title", "")
                or getattr(cand, "title", "")
                or getattr(cand, "alias", "")
                or tkey[2][:8]
            )
            snap = FrozenOptionSnapshot(
                target_key=tkey,
                has_cwd=has_cwd,
                cwd_snapshot=cwd_snap,
                role_snapshot=role_snap,
                title_snapshot=title_snap,
                question_id=qid,
                page=cur_page,
                label=lbl,
            )
            frozen_by_opt[(qid, cur_page, lbl)] = snap
            frozen_by_label.setdefault(lbl, {})[tkey] = snap
            orig_candidates[tkey] = cand

        rec = {
            "card_id": card_id,
            "channel_id": self._channel_id,
            "channel_secret": self._secret,
            "card": card,
            "project_id": pid,
            "role": rl,
            "page": cur_page,
            "frozen_options": frozen_by_opt,
            "frozen_by_label": frozen_by_label,
            "candidates_by_key": orig_candidates,
            "consumed": False,
        }
        _ISSUED_CARDS[card_id] = rec
        return rec

    def prompt_and_confirm(
        self,
        candidates: list[Any],
        *,
        project_id: str,
        role: str,
        ranked: list[pr.RankedCandidate] | None = None,
        scroll_cap: int = 60,
        page: int = 0,
        page_size: int = PAGE_SIZE,
        header_notice: str = "",
        receipt_id: str | None = None,
    ) -> tuple[str, Any, ps.HumanConfirmation | None, dict[str, Any]]:
        """Atomic host UI flow: render -> freeze options -> invoke real ask_user_fn -> sign receipt -> mint confirmation."""
        if self._closed or _ACTIVE_HOST_CHANNEL is not self:
            raise ps.SlotPermissionError("宿主发卡通道已关闭或非当前活跃宿主权威", reason="host_channel_closed")
        card = build_ranked_card_grouped(
            candidates,
            ranked=ranked,
            scroll_cap=scroll_cap,
            page=page,
            page_size=page_size,
            header_notice=header_notice,
        )
        self._register_card(card, project_id=project_id, role=role)
        raw_answers = self._ask_user_fn(card["questions"])
        if not isinstance(raw_answers, list):
            return ("cancelled", None, None, card)

        # Stamp answers with original card_id & page and seal via channel HMAC
        stamped_answers = []
        for a in raw_answers:
            if isinstance(a, dict):
                copy_a = dict(a)
                copy_a.setdefault("card_id", card["card_id"])
                copy_a.setdefault("page", card["page"])
                stamped_answers.append(copy_a)

        rid = ps._nonempty_str(receipt_id) or ("ui-" + uuid.uuid4().hex[:12])
        mac = _receipt_mac(self._secret, card["card_id"], card["page"], rid, stamped_answers)
        receipt = HostCardReceipt(
            card_id=card["card_id"],
            page=card["page"],
            receipt_id=rid,
            answers=tuple(stamped_answers),
            channel_id=self._channel_id,
            mac=mac,
        )
        action, chosen, conf = confirm_from_issued_card(
            card["card_id"],
            receipt,
            expected_page=card["page"],
        )
        return (action, chosen, conf, card)


def _status_tag(c: Any) -> str:
    status = getattr(c, "status", "") or ""
    return {
        "idle": "空闲",
        "running": "正忙",
        "busy_recent": "刚输入",
        "persisted_cold": "冷会话",
        "unknown": "",
    }.get(status, status)


def _cwd_short(cwd: str) -> str:
    p = pr._decode_path(cwd).rstrip("/\\")
    return p.split("/")[-1] if p else ""


def _group_of(c: Any) -> tuple[str, str]:
    meta = getattr(c, "meta", None) or {}
    proj = (meta.get("projectName") or "").strip() if isinstance(meta, dict) else ""
    return ("项目", proj) if proj else ("会话", "")


def _base_title(c: Any, *, is_ranked: bool) -> str:
    cid = getattr(c, "id", "")
    if is_ranked:
        raw = (
            getattr(c, "display_title", "")
            or getattr(c, "title", "")
            or getattr(c, "alias", "")
            or cid[:8]
        )
    else:
        raw = getattr(c, "title", "") or getattr(c, "alias", "") or cid[:8]
    if raw.startswith(REC_PREFIX.strip()) or raw in RESERVED_LABELS:
        return REC_ESCAPE_PREFIX + raw
    return raw


def _allocate_unique_label(base: str, c: Any, dup_base: bool, used: set[str]) -> str:
    c_leg, c_host, cid = ps.candidate_key(c)
    if not dup_base and base not in used:
        used.add(base)
        return base

    for k in range(4, max(4, len(cid)) + 1, 2):
        cand_label = "%s (%s)" % (base, cid[:k])
        if cand_label not in used:
            used.add(cand_label)
            return cand_label

    host_label = "%s (%s·%s)" % (base, c_host or "host", cid[:6])
    if host_label not in used:
        used.add(host_label)
        return host_label

    full_label = "%s (%s/%s·%s)" % (base, c_leg or "leg", c_host or "host", cid[:6])
    if full_label not in used:
        used.add(full_label)
        return full_label

    seq = 2
    while True:
        seq_label = "%s (%s/%s·%s #%d)" % (base, c_leg or "leg", c_host or "host", cid[:6], seq)
        if seq_label not in used:
            used.add(seq_label)
            return seq_label
        seq += 1


def build_ranked_card_grouped(
    candidates: list[Any],
    *,
    ranked: list[pr.RankedCandidate] | None = None,
    scroll_cap: int = 60,
    page: int = 0,
    page_size: int = PAGE_SIZE,
    header_notice: str = "",
    project_id: str = "",
    role: str = "",
) -> dict[str, Any]:
    """Pure Path A/B card renderer. NEVER registers a binding-eligible card into `_ISSUED_CARDS`."""
    card_id = "card-" + uuid.uuid4().hex[:16]
    is_ranked = ranked is not None
    if ranked is None:
        active = [c for c in candidates if ps._verify_candidate(c, allow_protected=False)[0]]
        ranked_list = [pr.RankedCandidate(candidate=c, score=0, raw_score=0) for c in active]
    else:
        ranked_list = [r for r in ranked if ps._verify_candidate(r.candidate, allow_protected=False)[0]]

    total = len(ranked_list)
    counts = Counter(_base_title(r.candidate, is_ranked=is_ranked) for r in ranked_list)

    base_rows: list[dict[str, Any]] = []
    used_labels: set[str] = set()

    for r in ranked_list:
        c = r.candidate
        c_leg, c_host, cid = ps.candidate_key(c)
        base = _base_title(c, is_ranked=is_ranked)
        dup = counts[base] > 1
        label = _allocate_unique_label(base, c, dup, used_labels)

        desc_parts = [
            "%s·%s" % (c_leg, getattr(c, "role", "")),
            _status_tag(c),
            _cwd_short(getattr(c, "cwd", "")),
        ]
        if r.bound_projects:
            desc_parts.append("已绑：%s" % "、".join(r.bound_projects))
        if r.reasons:
            desc_parts.append("理由：%s" % " / ".join(r.reasons))
        if dup:
            if c_host:
                desc_parts.append(c_host)
            if getattr(c, "lastActivity", ""):
                desc_parts.append(str(getattr(c, "lastActivity")))
            desc_parts.append(cid[:4])

        base_rows.append({
            "label": label,
            "description": " | ".join(p for p in desc_parts if p),
            "key": (c_leg, c_host, cid),
            "candidate": c,
            "ranked": r,
            "group": _group_of(c),
            "is_recommended_alias": False,
        })

    rec_source_rows = [row for row in base_rows if row["ranked"].recommended][: pr.MAX_RECOMMENDED]
    rec_alias_rows: list[dict[str, Any]] = []
    rec_options: list[dict[str, str]] = []
    for row in rec_source_rows:
        rec_label = REC_PREFIX + row["label"]
        if rec_label in used_labels:
            rec_label = _allocate_unique_label(rec_label, row["candidate"], True, used_labels)
        else:
            used_labels.add(rec_label)
        reason_str = " / ".join(row["ranked"].reasons) if row["ranked"].reasons else "综合匹配最高"
        rec_desc = "%s · 分数 %d —— 理由：%s" % (
            _status_tag(row["candidate"]) or "可用",
            row["ranked"].score,
            reason_str,
        )
        if row["ranked"].bound_projects:
            rec_desc += "（已绑：%s）" % "、".join(row["ranked"].bound_projects)
        rec_options.append({"label": rec_label, "description": rec_desc})
        rec_alias_rows.append({
            "label": rec_label,
            "description": rec_desc,
            "key": row["key"],
            "candidate": row["candidate"],
            "ranked": row["ranked"],
            "group": ("推荐", ""),
            "is_recommended_alias": True,
        })

    option_map: dict[tuple[str, int, str], Any] = {}
    visible_by_label: dict[str, dict[tuple[str, str, str], Any]] = {}
    visible_nav_by_q: dict[str, set[str]] = {}
    visible_rows: list[dict[str, Any]] = []

    def _register_visible_option(qid: str, cur_p: int, row: dict[str, Any]) -> None:
        lbl = row["label"]
        cand = row["candidate"]
        option_map[(qid, cur_p, lbl)] = cand
        visible_by_label.setdefault(lbl, {})[ps.candidate_key(cand)] = cand
        visible_rows.append(row)

    def _register_nav(qid: str, nav_lbl: str) -> None:
        visible_nav_by_q.setdefault(qid, set()).add(nav_lbl)

    notice = header_notice.strip()
    notice_attached = False

    def _consume_notice() -> str:
        nonlocal notice_attached
        if notice and not notice_attached:
            notice_attached = True
            return notice + "\n"
        return ""

    if total > scroll_cap:
        eff_page_size = max(1, int(page_size))
        pages = max(1, math.ceil(total / eff_page_size))
        cur_page = max(0, min(int(page), pages - 1))
        page_rows = base_rows[cur_page * eff_page_size : (cur_page + 1) * eff_page_size]

        questions: list[dict[str, Any]] = []
        if cur_page == 0 and rec_options:
            questions.append({
                "id": REC_GROUP_ID,
                "header": REC_HEADER,
                "question": "%s───── ★ 推荐目标 ─────\n仅作排序推荐，必须由您点击确认才投递；下方可翻页查看全部活跃窗或跳过不投" % _consume_notice(),
                "options": rec_options,
            })
            for arow in rec_alias_rows:
                _register_visible_option(REC_GROUP_ID, cur_page, arow)

        options = []
        for r in page_rows:
            options.append({"label": r["label"], "description": r["description"]})
            _register_visible_option("pick_window", cur_page, r)
        if cur_page < pages - 1:
            options.append({"label": NAV_NEXT, "description": "第%d/%d页" % (cur_page + 2, pages)})
            _register_nav("pick_window", NAV_NEXT)
        options.append({"label": NAV_KEYWORD, "description": "念几个独特字"})
        _register_nav("pick_window", NAV_KEYWORD)
        options.append({"label": NAV_CANCEL, "description": "放弃本次投递"})
        _register_nav("pick_window", NAV_CANCEL)
        questions.append({
            "id": "pick_window",
            "header": "选窗",
            "question": "%s投到哪个对话？（%d个活跃，第%d/%d页）" % (_consume_notice(), total, cur_page + 1, pages),
            "options": options,
        })
        return {
            "card_version": "v2",
            "card_id": card_id,
            "project_id": project_id.strip(),
            "role": role.strip(),
            "questions": questions,
            "rows": visible_rows,
            "all_rows": rec_alias_rows + base_rows,
            "option_map": option_map,
            "visible_by_label": visible_by_label,
            "visible_nav_by_q": visible_nav_by_q,
            "recommended_count": len(rec_options) if cur_page == 0 else 0,
            "grouped": False,
            "total": total,
            "page": cur_page,
            "pages": pages,
        }

    cur_page = 0
    questions = []
    if rec_options:
        questions.append({
            "id": REC_GROUP_ID,
            "header": REC_HEADER,
            "question": "%s───── ★ 推荐目标 ─────\n仅作排序推荐，必须由您点击确认才投递；下方仍可改选任意活跃窗或跳过不投" % _consume_notice(),
            "options": rec_options,
        })
        for arow in rec_alias_rows:
            _register_visible_option(REC_GROUP_ID, cur_page, arow)

    proj_rows = [r for r in base_rows if r["group"][0] == "项目"]
    sess_rows = [r for r in base_rows if r["group"][0] == "会话"]

    if proj_rows:
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in proj_rows:
            groups.setdefault(r["group"][1], []).append(r)
        for i, (gname, grp) in enumerate(groups.items()):
            qid = "grp_proj_%d" % i
            questions.append({
                "id": qid,
                "header": "项目 · %s" % gname,
                "question": "%s───── 项目 · %s ─────\n%d 个对话 · 选一个投；想不起名字直接打字筛；跳过或打「取消」＝不投" % (
                    _consume_notice(), gname, len(grp)
                ),
                "options": [{"label": r["label"], "description": r["description"]} for r in grp],
            })
            for r in grp:
                _register_visible_option(qid, cur_page, r)

    if sess_rows:
        order_hint = "已按匹配度排序" if is_ranked else "按最近活跃"
        questions.append({
            "id": "grp_session",
            "header": "会话区",
            "question": "%s───── 会话区 ─────\n独立会话（%d 个）· %s · 选一个投；想不起名字直接打字筛；跳过或打「取消」＝不投" % (
                _consume_notice(), len(sess_rows), order_hint
            ),
            "options": [{"label": r["label"], "description": r["description"]} for r in sess_rows],
        })
        for r in sess_rows:
            _register_visible_option("grp_session", cur_page, r)

    return {
        "card_version": "v2",
        "card_id": card_id,
        "project_id": project_id.strip(),
        "role": role.strip(),
        "questions": questions,
        "rows": visible_rows,
        "all_rows": rec_alias_rows + base_rows,
        "option_map": option_map,
        "visible_by_label": visible_by_label,
        "visible_nav_by_q": visible_nav_by_q,
        "recommended_count": len(rec_options),
        "grouped": True,
        "total": total,
        "page": cur_page,
        "pages": 1,
    }


def resolve_ranked_card(
    card: dict[str, Any],
    answers: list[dict[str, Any]],
    *,
    expected_card_id: str | None = None,
    expected_page: int | None = None,
    require_card_context: bool = False,
) -> tuple[str, Any]:
    """Resolve user answers strictly against options actually rendered on this card & page."""
    if require_card_context and (expected_card_id is None or expected_page is None):
        return ("missing_card_context", None)
    if expected_card_id is not None and card.get("card_id") != expected_card_id:
        return ("stale_card", None)
    cur_page = int(card.get("page") or 0)
    if expected_page is not None and cur_page != int(expected_page):
        return ("page_mismatch", None)

    omap = card.get("option_map") or {}
    vmap = card.get("visible_by_label") or {}
    nav_by_q = card.get("visible_nav_by_q") or {}
    all_visible_navs = {nav for navs in nav_by_q.values() for nav in navs}

    selected_by_key: dict[tuple[str, str, str], Any] = {}
    nav_action: tuple[str, Any] | None = None
    keyword_input: str | None = None
    unknown_hit = False

    for a in answers:
        qid = (a.get("id") or "").strip()
        ans_card_id = a.get("card_id")
        if require_card_context and ans_card_id != card.get("card_id"):
            return ("stale_card", None)
        if ans_card_id is not None and ans_card_id != card.get("card_id"):
            return ("stale_card", None)
        ans_page = a.get("page")
        if require_card_context and (ans_page is None or int(ans_page) != cur_page):
            return ("page_mismatch", None)
        if ans_page is not None and int(ans_page) != cur_page:
            return ("page_mismatch", None)

        custom = (a.get("custom") or "").strip()
        if custom:
            if custom in ("取消", "不投", "取消／不投", "cancel", "算了"):
                return ("cancelled", None)
            if keyword_input is None:
                keyword_input = custom
            continue

        sel = a.get("selected") or []
        for label in sel:
            if label == NAV_CANCEL:
                return ("cancelled", None)
            if label == NAV_NEXT:
                if (qid and label in (nav_by_q.get(qid) or set())) or (not qid and label in all_visible_navs):
                    nav_action = ("next_page", None)
                else:
                    unknown_hit = True
                continue
            if label == NAV_KEYWORD:
                if (qid and label in (nav_by_q.get(qid) or set())) or (not qid and label in all_visible_navs):
                    nav_action = ("keyword", None)
                else:
                    unknown_hit = True
                continue

            if qid:
                cand = omap.get((qid, cur_page, label))
                if cand is None:
                    unknown_hit = True
                else:
                    selected_by_key[ps.candidate_key(cand)] = cand
            else:
                matches = vmap.get(label) or {}
                if len(matches) == 1:
                    only_cand = next(iter(matches.values()))
                    selected_by_key[ps.candidate_key(only_cand)] = only_cand
                elif len(matches) > 1:
                    for k, c in matches.items():
                        selected_by_key[k] = c
                else:
                    unknown_hit = True

    if selected_by_key:
        if len(selected_by_key) > 1:
            return ("multi_selected", list(selected_by_key.values()))
        return ("ok", next(iter(selected_by_key.values())))

    if keyword_input is not None:
        return ("keyword", keyword_input)
    if nav_action is not None:
        return nav_action
    if unknown_hit:
        return ("not_found", None)
    return ("cancelled", None)


def confirm_from_issued_card(
    card_id: str,
    receipt: Any,
    *,
    expected_page: int,
    answer_ref: str | None = None,
    card_override: dict[str, Any] | None = None,
) -> tuple[str, Any, ps.HumanConfirmation | None]:
    """Verify a host-authenticated HostCardReceipt against a HostCardChannel-issued card.

    GPT v0.5 #1a & #2:
    - Rejects plain caller-crafted answer lists (`invalid_host_receipt`).
    - Rejects cards not issued through HostCardChannel (`stale_card`).
    - Verifies channel HMAC signature + card_id + page + single-use consumption.
    - Resolves strictly against frozen render-time option snapshots (`FrozenOptionSnapshot`)
      so post-render mutation of candidate objects cannot alter target_key / cwd / role.
    """
    if not isinstance(receipt, HostCardReceipt):
        return ("invalid_host_receipt", None, None)

    rec = _ISSUED_CARDS.get(card_id)
    if not rec:
        return ("stale_card", None, None)
    if rec.get("consumed"):
        return ("card_already_consumed", None, None)
    if card_override is not None and card_override.get("card_id") != card_id:
        return ("stale_card", None, None)
    if receipt.card_id != card_id or receipt.channel_id != rec["channel_id"]:
        return ("stale_card", None, None)
    if int(receipt.page) != int(expected_page) or int(receipt.page) != int(rec["page"]):
        return ("page_mismatch", None, None)

    expected_mac = _receipt_mac(
        rec["channel_secret"],
        receipt.card_id,
        receipt.page,
        receipt.receipt_id,
        list(receipt.answers),
    )
    if not hmac.compare_digest(receipt.mac, expected_mac):
        return ("invalid_host_receipt", None, None)

    # Resolve answers strictly against the FROZEN option snapshots captured at card render time
    frozen_opts: dict[tuple[str, int, str], FrozenOptionSnapshot] = rec["frozen_options"]
    frozen_by_label: dict[str, dict[tuple[str, str, str], FrozenOptionSnapshot]] = rec["frozen_by_label"]
    cur_page = int(rec["page"])

    selected_snaps: dict[tuple[str, str, str], FrozenOptionSnapshot] = {}
    for a in receipt.answers:
        if not isinstance(a, dict):
            continue
        if a.get("card_id") != card_id:
            return ("stale_card", None, None)
        if a.get("page") is None or int(a.get("page")) != cur_page:
            return ("page_mismatch", None, None)
        custom = (a.get("custom") or "").strip()
        if custom:
            if custom in ("取消", "不投", "取消／不投", "cancel", "算了"):
                return ("cancelled", None, None)
            return ("keyword", custom, None)
        qid = (a.get("id") or "").strip()
        for label in (a.get("selected") or []):
            if label == NAV_CANCEL:
                return ("cancelled", None, None)
            if label == NAV_NEXT:
                return ("next_page", None, None)
            if label == NAV_KEYWORD:
                return ("keyword", None, None)
            if qid:
                snap = frozen_opts.get((qid, cur_page, label))
                if snap is None:
                    return ("not_found", None, None)
                selected_snaps[snap.target_key] = snap
            else:
                hits = frozen_by_label.get(label) or {}
                if len(hits) != 1:
                    return ("not_found" if not hits else "multi_selected", None, None)
                only_snap = next(iter(hits.values()))
                selected_snaps[only_snap.target_key] = only_snap

    if not selected_snaps:
        return ("cancelled", None, None)
    if len(selected_snaps) > 1:
        return ("multi_selected", list(selected_snaps.keys()), None)

    chosen_snap = next(iter(selected_snaps.values()))
    chosen_cand = rec["candidates_by_key"].get(chosen_snap.target_key)

    rec["consumed"] = True
    cid = ps._nonempty_str(answer_ref) or receipt.receipt_id
    event = ps._create_host_card_event(
        project_id=rec["project_id"],
        role=rec["role"],
        target_key=chosen_snap.target_key,
        card_id=card_id,
        card_page=cur_page,
        confirmation_id=cid,
        has_frozen_cwd=chosen_snap.has_cwd,
        frozen_cwd=chosen_snap.cwd_snapshot,
        frozen_role=chosen_snap.role_snapshot,
    )
    conf = ps.issue_human_confirmation(host_event=event)
    return ("ok", chosen_cand, conf)
