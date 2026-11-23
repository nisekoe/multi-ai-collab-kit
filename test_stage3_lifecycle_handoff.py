# -*- coding: utf-8 -*-
"""Offline unit tests for stage3_lifecycle_handoff.py (v0.6).

Uses HostCardChannel (test-boundary host UI simulator) with single-use confirmation tokens.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

import card_renderer_v2 as card_v2
import picker_ranker as pr
import project_slots as ps
import stage3_lifecycle_handoff as s3


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
    busySecondsAgo: int | None = None
    reachable: bool = True
    unusableReason: str = ""
    truncated: bool = False
    meta: dict = field(default_factory=dict)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _ev_for(cand: FakeCandidate, msg_id: str = "msg-s3") -> ps.DeliveryEvidence:
    return ps.DeliveryEvidence(
        leg=ps.normalize_leg(cand.leg),
        target_key=ps.candidate_key(cand),
        evidence_type="porta_verified_step",
        message_id=msg_id,
        delivered_at="2026-11-23T01:40:00+08:00",
        verified=True,
        dry_run=False,
        route_only=False,
    )


def _mint_card_conf(pid: str, role: str, cand: FakeCandidate, cid: str | None = None) -> ps.HumanConfirmation:
    ch = card_v2.initialize_host_card_authority(
        lambda qs: [{"id": qs[0]["id"], "selected": [qs[0]["options"][0]["label"]]}]
    )
    try:
        act, chosen, conf, _card = ch.prompt_and_confirm([cand], project_id=pid, role=role, receipt_id=cid)
    finally:
        ch.close()
    assert act == "ok" and chosen is cand and conf is not None
    return conf


class TestStage3LifecycleAndHandoffV3(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self._tmp.name)
        _write_json(
            self.hub / "00-config" / "active_projects.json",
            {
                "active": [
                    {"project_id": "interconnect", "path": "20-projects/互联整合", "participants": ["desktop_bot", "review_bot", "gpt"], "project_rev": "1.1"},
                    {"project_id": "collab-test", "path": "20-projects/协作测试", "participants": ["desktop_bot", "review_bot"], "project_rev": "1"},
                ]
            },
        )
        _write_json(
            self.hub / "00-config" / "平台注册表.json",
            {
                "registry_version": "0.4.0",
                "platforms": {
                    "desktop_bot": {"leg": "harbor", "hostId": "local", "role": "desktop", "profile": "30-roles/desktop_bot"},
                    "review_bot": {"leg": "porta", "hostId": "antigravity", "role": "peer", "profile": "30-roles/review_bot"},
                    "gpt": {"leg": "codex", "hostId": "local", "role": "peer", "profile": "30-roles/gpt"},
                },
            },
        )
        (self.hub / "20-projects" / "互联整合").mkdir(parents=True, exist_ok=True)
        (self.hub / "20-projects" / "协作测试").mkdir(parents=True, exist_ok=True)
        (self.hub / "20-projects" / "协作测试" / "CONTEXT.md").write_text("# 协作测试\n", encoding="utf-8")
        (self.hub / "30-roles" / "review_bot").mkdir(parents=True, exist_ok=True)
        (self.hub / "30-roles" / "review_bot" / "profile.md").write_text("# review_bot\n", encoding="utf-8")
        self.store = ps.SlotStore(hub_root=self.hub)
        self.signals = pr.SignalStore(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _review_cand(self, cid: str, title: str = "评审助手窗") -> FakeCandidate:
        return FakeCandidate(leg="porta", hostId="antigravity", id=cid, title=title)

    def _bind_valid(self, pid: str, role: str, cand: FakeCandidate) -> None:
        conf = _mint_card_conf(pid, role, cand)
        self.store.bind_slot(pid, role, cand, confirmation=conf, delivery_evidence=_ev_for(cand), delivered=True)

    def test_closeout_precondition_blocks_still_active_and_corrupt_config(self) -> None:
        self._bind_valid("collab-test", "review_bot", self._review_cand("c-1"))
        self._bind_valid("interconnect", "review_bot", self._review_cand("c-2"))
        self.signals.record_pick("collab-test", "porta", "antigravity", "c-1")

        with self.assertRaises(ps.SlotValidationError) as ctx:
            s3.retire_slots_for_closed_project(self.store, "collab-test")
        self.assertEqual(ctx.exception.reason, "project_still_active")

        ap_path = self.hub / "00-config" / "active_projects.json"
        ap_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ps.SlotConfigError):
            s3.retire_slots_for_closed_project(self.store, "collab-test")
        self.assertEqual(self.store.load_slots_doc()["slots"]["collab-test:review_bot"]["status"], "active")

        _write_json(ap_path, {"active": [{"project_id": "interconnect", "path": "20-projects/互联整合", "participants": ["desktop_bot", "review_bot"]}]})
        res = s3.retire_slots_for_closed_project(self.store, "collab-test", reason="archived_to_90")
        self.assertEqual(res["retired_slots"], ["collab-test:review_bot"])
        self.assertTrue(res["signals_cleaned"])

    def test_ordinary_quote_of_marker_still_gets_handoff_header_and_idempotent(self) -> None:
        hdr = s3.build_handoff_context_header(self.store, "collab-test", "review_bot", handoff_kind="rebind")
        ordinary = "引用说明：检查 [战情室接棒指针 | 说明] 的文本标记"
        with_hdr = s3.prepend_handoff_header(ordinary, hdr)
        self.assertTrue(with_hdr.startswith(hdr))
        self.assertEqual(s3.prepend_handoff_header(with_hdr, hdr), with_hdr)

    def test_confirm_rebind_first_bind_rebind_and_single_use_host_confirmation(self) -> None:
        sent_texts: list[str] = []

        def fake_dispatch(*, candidate, text, dry_run, expected_cwd, registry_path):
            sent_texts.append(text)
            return ps.EXIT_OK, {"ok": True, "dry_run": False, "delivery_evidence": _ev_for(candidate, msg_id="m-%d" % len(sent_texts)).to_dict()}

        w1, w2 = self._review_cand("conv-w1", "一棒窗"), self._review_cand("conv-w2", "二棒窗")
        conf1 = _mint_card_conf("collab-test", "review_bot", w1)
        code1, pay1 = s3.confirm_rebind_and_deliver_with_handoff(
            self.store, "collab-test", "review_bot", w1, "第一条任务", fake_dispatch, confirmation=conf1, dry_run=False
        )
        self.assertEqual(code1, ps.EXIT_OK)
        self.assertTrue(pay1["handoff_attached"] and pay1["handoff_kind"] == "first_bind")

        # Reusing conf1 MUST fail (single-use)
        code_dup, pay_dup = s3.confirm_rebind_and_deliver_with_handoff(
            self.store, "collab-test", "review_bot", w1, "重复凭证", fake_dispatch, confirmation=conf1, dry_run=False
        )
        self.assertEqual(code_dup, ps.EXIT_UNQUALIFIED)
        self.assertEqual(pay_dup["reason"], "confirmation_already_used")

        # Fresh confirmation for same active window -> no duplicate handoff header
        conf1b = _mint_card_conf("collab-test", "review_bot", w1)
        code2, pay2 = s3.confirm_rebind_and_deliver_with_handoff(
            self.store, "collab-test", "review_bot", w1, "同窗第二条", fake_dispatch, confirmation=conf1b, dry_run=False
        )
        self.assertEqual(code2, ps.EXIT_OK)
        self.assertFalse(pay2["handoff_attached"])

        # Rebind to w2 -> attaches rebind pointer
        conf2 = _mint_card_conf("collab-test", "review_bot", w2)
        code3, pay3 = s3.confirm_rebind_and_deliver_with_handoff(
            self.store, "collab-test", "review_bot", w2, "接棒任务", fake_dispatch, confirmation=conf2, dry_run=False
        )
        self.assertEqual(code3, ps.EXIT_OK)
        self.assertTrue(pay3["handoff_attached"] and pay3["handoff_kind"] == "rebind")
        self.assertIn("前窗=conv-w1", sent_texts[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
