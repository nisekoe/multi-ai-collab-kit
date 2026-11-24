"""Porta 腿的投前绑定判定：最新放行、旧绑定要确认、无绑定要确认。"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(REPO, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "tools"))

import porta_adapter as pa  # noqa: E402

ROWS = [
    {"id": "aaaa1111-0000", "last": "2026-11-24T03:00:00"},
    {"id": "bbbb2222-0000", "last": "2026-11-23T10:00:00"},
]


class BindingVerdictTest(unittest.TestCase):
    def test_chosen_newest_passes(self):
        v = pa.binding_verdict("bbbb2222-0000", "aaaa1111-0000", ROWS)
        self.assertTrue(v["ok"])
        self.assertEqual(v["reason"], "chosen_newest")

    def test_old_binding_with_newer_needs_confirm(self):
        v = pa.binding_verdict("bbbb2222-0000", "bbbb2222-0000", ROWS)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "new_window_needs_confirm")
        self.assertEqual(v["newer"]["id"], "aaaa1111-0000")

    def test_no_binding_needs_confirm(self):
        v = pa.binding_verdict("", "bbbb2222-0000", ROWS)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "no_binding_need_confirm")

    def test_chosen_not_bound(self):
        rows = ROWS + [{"id": "cccc3333-0000", "last": "2026-11-22T10:00:00"}]
        v = pa.binding_verdict("aaaa1111-0000", "cccc3333-0000", rows)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "chosen_not_bound")

    def test_binding_missing(self):
        v = pa.binding_verdict("cccc3333-0000", "bbbb2222-0000", ROWS)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "binding_missing")

    def test_bound_is_newest_passes(self):
        v = pa.binding_verdict("aaaa1111-0000", "aaaa1111-0000", ROWS)
        self.assertTrue(v["ok"])

    # F1: 无关新窗放行并提示；相关新窗拦截；人点名放行并带记录
    def test_f1_unrelated_newer_passes_with_notice(self):
        rows = [
            {"id": "5f47761b-0000", "last": "2026-11-24T22:15:00Z", "cwd": "X:/work/project-b", "title": "0.6.0 DSH"},
            {"id": "654ad433-0000", "last": "2026-11-24T21:30:00Z", "cwd": "X:/work/project-a", "title": "规则审阅"},
        ]
        v = pa.binding_verdict("654ad433-0000", "654ad433-0000", rows)
        self.assertTrue(v["ok"])
        self.assertEqual(v["reason"], "bound_target_newer_unrelated")
        self.assertEqual(len(v["ignored_newer"]), 1)
        self.assertIn("无关新窗", v["notice"])

    def test_f1_related_newer_blocks_and_human_confirm_passes(self):
        rows = [
            {"id": "99990000-0000", "last": "2026-11-24T22:20:00Z", "cwd": "X:/work/project-a", "title": "规则审阅续"},
            {"id": "654ad433-0000", "last": "2026-11-24T21:30:00Z", "cwd": "X:/work/project-a", "title": "规则审阅"},
        ]
        blocked = pa.binding_verdict("654ad433-0000", "654ad433-0000", rows)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "new_window_needs_confirm")

        confirmed = pa.binding_verdict(
            "654ad433-0000", "654ad433-0000", rows,
            confirmed=True, confirmed_by="card_pick", confirmed_basis="card:654ad433",
        )
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["reason"], "human_confirmed")
        self.assertEqual(confirmed["confirmed_by"], "card_pick")
        self.assertEqual(confirmed["confirmed_basis"], "card:654ad433")

    # F2: 已归档但时间更晚的会话被统一排除
    def test_f2_archived_newer_is_excluded(self):
        rows = [
            {"id": "arch9999-0000", "last": "2026-11-24T23:00:00Z", "archived": True},
            {"id": "654ad433-0000", "last": "2026-11-24T21:30:00Z", "archived": False},
        ]
        v = pa.binding_verdict("654ad433-0000", "654ad433-0000", rows)
        self.assertTrue(v["ok"])
        self.assertEqual(v["reason"], "chosen_newest")

    # F3: 新建空窗(stepCount=0且无last)不顶掉在用绑定；活跃旧线排最前
    def test_f3_empty_new_window_does_not_override_active_binding(self):
        rows = [
            {"id": "empty111-0000", "created": "2026-11-24T22:30:00Z", "last": "", "stepCount": 0},
            {"id": "654ad433-0000", "created": "2026-11-20T10:00:00Z", "last": "2026-11-24T22:00:00Z", "stepCount": 12},
        ]
        v = pa.binding_verdict("654ad433-0000", "654ad433-0000", rows)
        self.assertTrue(v["ok"])
        self.assertEqual(v["reason"], "chosen_newest")

    # F6: 档位标签自动补齐与幂等
    def test_f6_ensure_tier_tag_auto_fill_and_idempotent(self):
        import msg_envelope as me
        raw = "【审阅通气】请核查这段内容"
        patched, filled = me.ensure_tier_tag(raw)
        self.assertTrue(filled)
        self.assertTrue(patched.startswith("[判断] "))

        already = "[速答] 已完成"
        patched2, filled2 = me.ensure_tier_tag(already)
        self.assertFalse(filled2)
        self.assertEqual(patched2, already)

    # F7: ask_card --from-porta --send-text 选完直接带人点名投递
    def test_f7_ask_card_from_porta_send_one_shot(self):
        import contextlib
        import io
        import json
        from unittest import mock
        import ask_card

        groups = [{"title": "会话区", "kind": "session", "options": [
            {"id": "654ad433-0000", "label": "审阅窗", "description": "活", "tags": ["空闲"]},
        ]}]
        fake_adapter = mock.Mock()
        fake_adapter.deliver.return_value = (0, {"ok": True, "status": "consumed", "bindingVerdict": "human_confirmed"})
        buf = io.StringIO()
        with mock.patch.object(ask_card, "porta_groups", return_value=groups), \
                mock.patch.object(ask_card, "choose_from_groups", return_value=groups[0]["options"][0]), \
                mock.patch.object(pa, "PortaAdapter", return_value=fake_adapter), \
                contextlib.redirect_stdout(buf):
            code = ask_card.main(["--from-porta", "--send-text", "测试直投", "--peer-agent", "desktop_bot", "--no-wait"])
        self.assertEqual(code, 0)
        kwargs = fake_adapter.deliver.call_args.kwargs
        self.assertTrue(kwargs["confirmed"])
        self.assertEqual(kwargs["confirmed_by"], "card_pick")
        out = json.loads(buf.getvalue())
        self.assertEqual(out["target"], "654ad433-0000")
        self.assertTrue(out["delivery"]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)