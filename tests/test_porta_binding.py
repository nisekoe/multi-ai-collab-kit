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
        v = pa.binding_verdict("cccc3333-0000", "bbbb2222-0000", ROWS)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "chosen_not_bound")

    def test_bound_is_newest_passes(self):
        v = pa.binding_verdict("aaaa1111-0000", "aaaa1111-0000", ROWS)
        self.assertTrue(v["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)