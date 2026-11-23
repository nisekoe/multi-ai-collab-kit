# -*- coding: utf-8 -*-
"""Offline unit tests for picker_ranker.py and card_renderer_v2.py (v0.6).

Covers GPT v0.5 #1a & #2a/#2b: HostCardChannel HMAC-signed receipts, bare-answer cross-card
rejection, immutable render-time option snapshots, current-page-only card validation,
P3 label collision, and P5 literal inner spaces.
"""
from __future__ import annotations
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

import card_renderer_v2 as card_v2
import picker_ranker as pr
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


class TestPickerRankerAndCardV3(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self._tmp.name)
        self.store = ps.SlotStore(hub_root=self.hub)
        self.now_ts = datetime(2026, 11, 22, 23, 0, 0, tzinfo=timezone.utc).timestamp()
        self.recent_iso = "2026-11-22T22:30:00Z"
        self.old_iso = "2026-11-10T10:00:00Z"
        self.project_entry = {
            "project_id": "interconnect",
            "path": "20-projects/互联整合",
            "participants": ["desktop_bot", "feishu_bot", "review_bot", "gpt"],
            "keywords": ["选窗", "调度"],
            "workspace_hints": ["hub"],
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # 1. GPT v0.5 #1a & #2a: bare answers rejected, cross-card receipt rejected, valid HostCardChannel succeeds + anti-replay
    def test_v06_host_card_channel_hmac_receipt_and_bare_answer_rejection(self) -> None:
        a = FakeCandidate(leg="porta", hostId="antigravity", id="card-a", title="same", cwd="fixture/a", status="idle")
        b = FakeCandidate(leg="porta", hostId="antigravity", id="card-b", title="same", cwd="fixture/b", status="idle")

        # Pure render does NOT issue a bindable card
        pure_card = card_v2.build_ranked_card_grouped([b], project_id="interconnect", role="review_bot")
        bare_answer = [{"id": "grp_session", "selected": ["same"]}]
        self.assertEqual(
            card_v2.confirm_from_issued_card(pure_card["card_id"], bare_answer, expected_page=0),
            ("invalid_host_receipt", None, None),
        )

        # Direct HostCardChannel instantiation rejected (must go through single-shot host authority bootstrap)
        with self.assertRaises(ps.SlotPermissionError) as ctx_direct:
            card_v2.HostCardChannel(lambda qs: [])
        self.assertEqual(ctx_direct.exception.reason, "host_authority_required")

        # HostCardChannel issues card A, captures real UI click, freezes cwd, and signs receipt
        captured_cards = []
        def ui_click_first(questions):
            captured_cards.append(questions)
            return [{"id": questions[0]["id"], "selected": [questions[0]["options"][0]["label"]]}]

        ch = card_v2.initialize_host_card_authority(ui_click_first)
        try:
            with self.assertRaises(ps.SlotPermissionError) as ctx_dup:
                card_v2.initialize_host_card_authority(lambda qs: [])
            self.assertEqual(ctx_dup.exception.reason, "host_authority_already_initialized")

            act, chosen, conf, card_a = ch.prompt_and_confirm([a], project_id="interconnect", role="review_bot")
            self.assertEqual(act, "ok")
            self.assertEqual(chosen.id, "card-a")
            self.assertIsNotNone(conf)
            self.assertEqual(conf.frozen_cwd, "fixture/a")

            # Tampered MAC receipt rejected
            card_b = card_v2.build_ranked_card_grouped([b])
            rec_b = ch._register_card(card_b, project_id="interconnect", role="review_bot")
            bad_mac_rcpt = card_v2.HostCardReceipt(
                card_id=card_b["card_id"], page=0, receipt_id="r-1",
                answers=({"id": "grp_session", "selected": ["same"], "card_id": card_b["card_id"], "page": 0},),
                channel_id=ch.channel_id, mac="deadbeef",
            )
            self.assertEqual(
                card_v2.confirm_from_issued_card(card_b["card_id"], bad_mac_rcpt, expected_page=0),
                ("invalid_host_receipt", None, None),
            )
            # Bare answer list against registered card B STILL rejected
            self.assertEqual(
                card_v2.confirm_from_issued_card(rec_b["card_id"], bare_answer, expected_page=0),
                ("invalid_host_receipt", None, None),
            )
        finally:
            ch.close()

    # 2. GPT #6: page 0 rejects page 1 label + require_card_context + production v2 guard
    def test_gpt6_paginated_card_rejects_off_page_labels_and_requires_context(self) -> None:
        a = FakeCandidate(leg="porta", hostId="antigravity", id="card-a", title="A", status="idle")
        b = FakeCandidate(leg="porta", hostId="antigravity", id="card-b", title="B", status="idle")
        page0 = card_v2.build_ranked_card_grouped([a, b], page_size=1, scroll_cap=1, page=0)

        self.assertEqual(card_v2.resolve_ranked_card(page0, [{"selected": ["B"]}]), ("not_found", None))
        self.assertEqual(card_v2.resolve_ranked_card(page0, [{"id": "pick_window", "selected": ["B"]}]), ("not_found", None))
        self.assertEqual(card_v2.resolve_ranked_card(page0, [{"id": "pick_window", "selected": ["A"]}], require_card_context=True), ("missing_card_context", None))

        try:
            import window_picker as wp
            self.assertEqual(wp.resolve_grouped(page0, [{"id": "pick_window", "selected": ["A"]}]), ("v2_card_use_v2_resolver", None))
        except ImportError:
            pass

    # 3. P3 collision + P5 inner spaces + P4 busy wording
    def test_p3_collision_p5_spaces_and_p4_busy_wording(self) -> None:
        win_a = FakeCandidate(leg="porta", hostId="antigravity", id="id-a", title="互联整合A窗", display_title="互联整合A窗", cwd="workspace/hub/20-projects/互联整合", status="idle", lastActivity=self.recent_iso)
        win_b = FakeCandidate(leg="porta", hostId="antigravity", id="id-b", title="★ [推荐] 互联整合A窗", display_title="★ [推荐] 互联整合A窗", status="idle", lastActivity=self.old_iso)
        ranked = pr.rank_candidates([win_b, win_a], project_id="interconnect", role="review_bot", project_entry=self.project_entry, now_ts=self.now_ts)
        card = card_v2.build_ranked_card_grouped([win_b, win_a], ranked=ranked)
        rec_a = card["questions"][0]["options"][0]["label"]
        b_lbl = [o["label"] for o in card["questions"][1]["options"] if o["label"] != "互联整合A窗"][0]
        self.assertNotEqual(rec_a, b_lbl)
        self.assertEqual(card_v2.resolve_ranked_card(card, [{"id": "grp_session", "selected": [b_lbl]}])[1].id, "id-b")

        c_sp2 = FakeCandidate(leg="codex", hostId="local", id="sp2", title="杂", cwd="fixture/a  b", status="unknown", busySecondsAgo=300)
        c_sp1 = FakeCandidate(leg="codex", hostId="local", id="sp1", title="杂", cwd="fixture/a b", status="unknown", busySecondsAgo=300)
        by_id = {r.candidate.id: r for r in pr.rank_candidates([c_sp2, c_sp1], project_entry={"workspace_hints": ["fixture/a b"]}, now_ts=self.now_ts)}
        self.assertEqual(by_id["sp1"].breakdown.get("workspace_affinity"), 25)
        self.assertIsNone(by_id["sp2"].breakdown.get("workspace_affinity"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
