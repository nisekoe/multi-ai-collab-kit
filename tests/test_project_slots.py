# -*- coding: utf-8 -*-
"""Offline unit tests for project_slots.py and picker_slots_orchestrator.py (v0.6).

Covers all GPT v0.5 final review findings (#1a/#1b/#1c, #2a/#2b, #3) + full regression suite.
Uses HostCardChannel (test-boundary host UI simulator) and sys.stdin patching at test boundary only.
"""
from __future__ import annotations
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import io
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import patch

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

import card_renderer_v2 as card_v2
import picker_slots_orchestrator as orch
import project_slots as ps


@dataclass
class FakeCandidate:
    leg: str
    hostId: str
    id: str
    title: str = ""
    alias: str = ""
    display_title: str = ""
    role: str = "peer"
    cwd: str = ""
    status: str = "idle"
    lastActivity: str = ""
    busySecondsAgo: int | float | bool | None = None
    reachable: bool = True
    unusableReason: str = ""
    truncated: bool = False
    meta: dict = field(default_factory=dict)


class _FakeTTYStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _ev_for(cand: FakeCandidate, *, msg_id: str = "msg-001", verified: bool = True,
            dry_run: bool = False, route_only: bool = False,
            evidence_type: str | None = None) -> ps.DeliveryEvidence:
    leg = ps.normalize_leg(cand.leg)
    ev_type = evidence_type or ps.LEG_TO_EVIDENCE_TYPE.get(leg, "porta_verified_step")
    return ps.DeliveryEvidence(
        leg=leg,
        target_key=ps.candidate_key(cand),
        evidence_type=ev_type,
        message_id=msg_id,
        delivered_at="2026-11-23T01:35:00+08:00",
        verified=verified,
        dry_run=dry_run,
        route_only=route_only,
    )


def _full_leg_status(leg: str, host: str, role: str) -> dict[str, dict]:
    return {leg: {"ok": True, "truncated": False, "error": "", "hosts": [host], "roles": [role]}}


def _mint_via_host_channel(project_id: str, role: str, cand: FakeCandidate, *, cid: str | None = None) -> ps.HumanConfirmation:
    """Host-boundary UI simulator invoking single-shot initialize_host_card_authority."""
    def fake_host_ui(questions: list[dict]) -> list[dict]:
        q = questions[0]
        return [{"id": q["id"], "selected": [q["options"][0]["label"]]}]

    channel = card_v2.initialize_host_card_authority(fake_host_ui)
    try:
        act, chosen, conf, _card = channel.prompt_and_confirm(
            [cand], project_id=project_id, role=role, receipt_id=cid,
        )
    finally:
        channel.close()
    assert act == "ok" and chosen is cand and conf is not None
    return conf


class TestProjectSlotsV3(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self._tmp.name)
        self._tick = 0

        _write_json(
            self.hub / "00-config" / "active_projects.json",
            {
                "active": [
                    {
                        "project_id": "interconnect",
                        "path": "20-projects/互联整合",
                        "participants": ["desktop_bot", "feishu_bot", "review_bot", "gpt"],
                        "project_rev": "1.1",
                    },
                    {
                        "project_id": "sharing",
                        "path": "20-projects/分享",
                        "participants": ["desktop_bot", "review_bot"],
                        "project_rev": "0.2",
                        "status": "paused",
                    },
                    {
                        "project_id": "collab-test",
                        "path": "20-projects/协作测试",
                        "participants": ["desktop_bot", "review_bot", "gpt"],
                        "project_rev": "1",
                    },
                ]
            },
        )
        _write_json(
            self.hub / "00-config" / "平台注册表.json",
            {
                "registry_version": "0.4.0",
                "platforms": {
                    "desktop_bot": {"leg": "harbor", "hostId": "local", "role": "desktop", "adapter": "harbor"},
                    "feishu_bot": {"leg": "harbor", "hostId": "local", "role": "feishu", "adapter": "harbor"},
                    "review_bot": {"leg": "porta", "hostId": "antigravity", "role": "peer", "adapter": "porta"},
                    "gpt": {
                        "leg": "codex",
                        "hostId": "local",
                        "role": "peer",
                        "adapter": "codex-queue",
                        "session_registry": "_state/window_registry/GPT桌面端会话登记.json",
                    },
                },
            },
        )
        _write_json(
            self.hub / "_state" / "window_registry" / "GPT桌面端会话登记.json",
            {"lines": []},
        )
        self.store = ps.SlotStore(hub_root=self.hub, clock=self._next_clock)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _next_clock(self) -> str:
        self._tick += 1
        return "2026-11-23T01:40:%02d+08:00" % self._tick

    def _review_cand(self, cid: str = "conv-main-001", title: str = "选窗器智能调度", **kw) -> FakeCandidate:
        return FakeCandidate(leg="porta", hostId="antigravity", id=cid, title=title, **kw)

    def _gpt_cand(self, cid: str = "thread-gpt-001", title: str = "Codex联调",
                  cwd: str = "workspace/hub", status: str = "unknown",
                  busySecondsAgo: int | float | bool | None = 120, **kw) -> FakeCandidate:
        return FakeCandidate(
            leg="codex", hostId="local", id=cid, title=title, cwd=cwd,
            status=status, busySecondsAgo=busySecondsAgo, **kw,
        )

    def _desktop_cand(self, cid: str = "session-desktop_bot-001", title: str = "桌面助手主窗", **kw) -> FakeCandidate:
        return FakeCandidate(leg="harbor", hostId="local", id=cid, title=title, role="desktop", **kw)

    def _ning_cand(self, cid: str = "session-feishu_bot-001", title: str = "飞书助手飞书窗", **kw) -> FakeCandidate:
        return FakeCandidate(leg="harbor", hostId="local", id=cid, title=title, role="feishu_bot", **kw)

    def _bind_valid(self, project_id: str, role: str, cand: FakeCandidate,
                    actor: str = "human_card", cid: str | None = None) -> dict:
        if actor == "human_cli":
            with patch.object(sys, "stdin", _FakeTTYStdin(cand.id[:8] + "\n")), \
                 patch.object(sys, "stdout", io.StringIO()):
                conf = ps.issue_confirmation_from_cli_tty(
                    project_id=project_id, role=role, candidate=cand, confirmation_id=cid,
                )
        else:
            conf = _mint_via_host_channel(project_id, role, cand, cid=cid)
        return self.store.bind_slot(
            project_id, role, cand,
            confirmation=conf, delivery_evidence=_ev_for(cand), delivered=True,
        )

    # 1. GPT v0.5 #1a/#1b/#1c: public card API cannot self-mint; CLI rejects injected callbacks; confirmation is strictly single-use
    def test_v06_1_host_only_card_real_cli_tty_and_single_use_confirmation(self) -> None:
        w = self._review_cand("target-a")

        # #1a: Public build_ranked_card_grouped does NOT register into _ISSUED_CARDS, and raw answer list is rejected
        public_card = card_v2.build_ranked_card_grouped([w], project_id="interconnect", role="review_bot")
        raw_ans = [{"id": public_card["questions"][0]["id"], "selected": [public_card["questions"][0]["options"][0]["label"]]}]
        self.assertEqual(
            card_v2.confirm_from_issued_card(public_card["card_id"], raw_ans, expected_page=0),
            ("invalid_host_receipt", None, None),
        )
        # Even a forged HostCardReceipt cannot use an unissued public card
        forged_rcpt = card_v2.HostCardReceipt(
            card_id=public_card["card_id"], page=0, receipt_id="fake",
            answers=tuple(raw_ans), channel_id="hch-fake", mac="00" * 32,
        )
        self.assertEqual(
            card_v2.confirm_from_issued_card(public_card["card_id"], forged_rcpt, expected_page=0),
            ("stale_card", None, None),
        )

        # #1a-bootstrap: Direct HostCardChannel(...) & duplicate initialize_host_card_authority rejected
        with self.assertRaises(ps.SlotPermissionError) as ctx_direct:
            card_v2.HostCardChannel(lambda qs: [])
        self.assertEqual(ctx_direct.exception.reason, "host_authority_required")

        ch_boot = card_v2.initialize_host_card_authority(lambda qs: [])
        try:
            with self.assertRaises(ps.SlotPermissionError) as ctx_dup:
                card_v2.initialize_host_card_authority(lambda qs: [])
            self.assertEqual(ctx_dup.exception.reason, "host_authority_already_initialized")
        finally:
            ch_boot.close()

        with self.assertRaises(ps.SlotPermissionError) as ctx_closed:
            ch_boot.prompt_and_confirm([w], project_id="interconnect", role="review_bot")
        self.assertEqual(ctx_closed.exception.reason, "host_channel_closed")

        # #1b: Production CLI API has NO injectable isatty_fn / input_fn parameters
        with self.assertRaises(TypeError):
            ps.issue_confirmation_from_cli_tty(
                project_id="interconnect", role="review_bot", candidate=w,
                isatty_fn=lambda: True, input_fn=lambda _p: w.id[:8],  # type: ignore[call-arg]
            )
        # Non-TTY stdin rejected
        with patch.object(sys, "stdin", io.StringIO(w.id[:8] + "\n")):
            with self.assertRaises(ps.SlotPermissionError) as ctx_tty:
                ps.issue_confirmation_from_cli_tty(project_id="interconnect", role="review_bot", candidate=w)
            self.assertEqual(ctx_tty.exception.reason, "cli_not_interactive_tty")

        # Real TTY stdin (mocked at test boundary) succeeds
        with patch.object(sys, "stdin", _FakeTTYStdin(w.id[:8] + "\n")), patch.object(sys, "stdout", io.StringIO()):
            cli_conf = ps.issue_confirmation_from_cli_tty(project_id="interconnect", role="review_bot", candidate=w)
        self.assertEqual(cli_conf.actor, "human_cli")

        # #1c: Single-use confirmation — first live dispatch succeeds, second dispatch with SAME token rejected (0 posts!)
        posts = []
        def spy_dispatch(*, candidate, **kw):
            posts.append(candidate.id)
            return 0, {"ok": True, "delivery_evidence": _ev_for(candidate, msg_id="m-%d" % len(posts)).to_dict()}

        rc1, out1 = orch.confirm_and_deliver_slot(
            self.store, "interconnect", "review_bot", w, "first", spy_dispatch, confirmation=cli_conf, dry_run=False
        )
        self.assertEqual(rc1, ps.EXIT_OK)
        self.assertTrue(out1["bound"])
        self.assertEqual(len(posts), 1)

        rc2, out2 = orch.confirm_and_deliver_slot(
            self.store, "interconnect", "review_bot", w, "second-replay", spy_dispatch, confirmation=cli_conf, dry_run=False
        )
        self.assertEqual(rc2, ps.EXIT_UNQUALIFIED)
        self.assertFalse(out2["bound"])
        self.assertEqual(out2["reason"], "confirmation_already_used")
        self.assertEqual(len(posts), 1)  # ZERO second dispatch!

    # 2. GPT v0.5 #2b: post-render mutation of candidate.cwd rejected as cwd_drift (0 posts!)
    def test_v06_2_post_render_cwd_mutation_rejected_before_dispatch(self) -> None:
        mutable = self._review_cand("scoped-window", cwd="fixture/at-render")
        conf = _mint_via_host_channel("interconnect", "review_bot", mutable)
        self.assertTrue(conf.has_frozen_cwd)
        self.assertEqual(conf.frozen_cwd, "fixture/at-render")

        # Mutate candidate cwd AFTER card render
        mutable.cwd = "fixture/after-render"
        posts = []
        rc, out = orch.confirm_and_deliver_slot(
            self.store, "interconnect", "review_bot", mutable, "hi",
            lambda **kw: posts.append(kw) or (0, {"ok": True}),
            confirmation=conf, dry_run=False,
        )
        self.assertEqual(rc, ps.EXIT_UNQUALIFIED)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "cwd_drift")
        self.assertEqual(posts, [])

    # 3. GPT v0.5 #3: CAS strictly separates "row absent" from "newer row that became stale"
    def test_v06_3_late_confirm_from_absent_does_not_overwrite_newer_stale_binding(self) -> None:
        a, b = self._review_cand("a"), self._review_cand("b")
        old_conf = _mint_via_host_channel("interconnect", "review_bot", a)

        def race_bind_and_stale(*, candidate, **kw):
            newer = self._bind_valid("interconnect", "review_bot", b)
            applied, row = self.store.mark_slot_stale(
                "interconnect", "review_bot", "workspace_protected",
                expected_gen=newer["binding_gen"], expected_target_key=ps.candidate_key(b),
            )
            self.assertTrue(applied and row["status"] == "stale")
            return 0, {"ok": True, "delivery_evidence": _ev_for(candidate, msg_id="late-a").to_dict()}

        rc, out = orch.confirm_and_deliver_slot(
            self.store, "interconnect", "review_bot", a, "hi", race_bind_and_stale,
            confirmation=old_conf, dry_run=False,
        )
        row = self.store.load_slots_doc()["slots"]["interconnect:review_bot"]
        self.assertEqual(rc, ps.EXIT_OK)
        self.assertFalse(out["bound"])
        self.assertEqual(out["action"], "delivered_to_superseded_confirm")
        self.assertTrue(out["do_not_retry"])
        # Newer B and its STALE status MUST be preserved untouched!
        self.assertEqual(row["target"]["id"], b.id)
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["stale_reason"], "workspace_protected")

    # 4. Compare-to-write interleaved CAS + empty cwd_snapshot="" preserved + authoritative error/unknown role/target_key
    def test_core_cas_empty_cwd_and_authority_regressions(self) -> None:
        a, b = self._review_cand("a", cwd=""), self._review_cand("b", cwd="")
        self._bind_valid("interconnect", "review_bot", a)

        # Interleaved compare-to-write blocked
        orig_save = self.store.save_slots_doc
        raced = []
        def save_interleaved(doc, *, cas=None):
            if not raced and doc["slots"]["interconnect:review_bot"]["status"] == "stale":
                raced.append(True)
                self._bind_valid("interconnect", "review_bot", b)
            return orig_save(doc, cas=cas)

        with patch.object(self.store, "save_slots_doc", side_effect=save_interleaved):
            rc, out = orch.deliver_by_slot(
                self.store, "interconnect", "review_bot", "hi",
                lambda registry_path=None: {"candidates": [a], "legs": _full_leg_status("porta", "antigravity", "peer")},
                lambda **kw: (ps.EXIT_UNQUALIFIED, {"ok": False, "reason": "workspace_protected", "target_key": list(ps.candidate_key(a))}),
                dry_run=False,
            )
        self.assertTrue(raced)
        self.assertEqual(out["action"], "slot_rebound")
        self.assertEqual(self.store.load_slots_doc()["slots"]["interconnect:review_bot"]["target"]["id"], b.id)

        # Explicit empty cwd_snapshot="" preserved to bridge and drift detected
        seen_cwds = []
        orch.deliver_by_slot(
            self.store, "interconnect", "review_bot", "hi",
            lambda registry_path=None: {"candidates": [b], "legs": _full_leg_status("porta", "antigravity", "peer")},
            lambda *, candidate, expected_cwd, **kw: seen_cwds.append(expected_cwd) or (0, {"ok": True, "delivery_evidence": _ev_for(candidate).to_dict()}),
            dry_run=False,
        )
        self.assertEqual(seen_cwds, [""])
        self.assertEqual(
            self.store.resolve_slot_route("interconnect", "review_bot", [replace(b, cwd="fixture/new")]).reason,
            "stale:cwd_drift",
        )

        # Missing error / error=None / matched role=unknown / exit6 missing target_key -> need_evidence
        nominal = {"ok": True, "truncated": False, "error": "", "hosts": ["antigravity"], "roles": ["peer"]}
        self.assertEqual(
            self.store.resolve_slot_route("interconnect", "review_bot", [], mutate=True, leg_status={"porta": dict(nominal, error=None)}).action,
            "need_evidence",
        )
        self.assertEqual(
            self.store.resolve_slot_route("interconnect", "review_bot", [replace(b, role="unknown")], mutate=True, leg_status={"porta": nominal}).reason,
            "need_evidence:identity_missing",
        )
        rc_nk, out_nk = orch.deliver_by_slot(
            self.store, "interconnect", "review_bot", "hi",
            lambda registry_path=None: {"candidates": [b], "legs": {"porta": nominal}},
            lambda **kw: (ps.EXIT_UNQUALIFIED, {"ok": False, "reason": "workspace_protected"}),
            dry_run=False,
        )
        self.assertEqual((rc_nk, out_nk["action"]), (ps.EXIT_ENV, "need_evidence"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
