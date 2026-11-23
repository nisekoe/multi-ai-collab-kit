"""G-54 四组小样：同时来件各回原窗、同标题撞旧绑定、停止后队列项、同号正文未结束。

四组都只测判定与信封层，不发真消息、不耗额度。
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(REPO, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "tools"))

import msg_envelope as M  # noqa: E402
import route_policy as R  # noqa: E402


class G54Test(unittest.TestCase):
    def test_two_simultaneous_messages_reply_to_own_window(self):
        """两条同时来件：各自带自己的回程地址，回话按地址回，不串。"""
        a_from = M.addr("harbor", "local", "session-aaa")
        b_from = M.addr("harbor", "local", "session-bbb")
        env_a = M.new_envelope(task_id="t1", reply_to=a_from, peer_agent="momo", from_addr=a_from)
        env_b = M.new_envelope(task_id="t1", reply_to=b_from, peer_agent="momo", from_addr=b_from)
        self.assertNotEqual(a_from, b_from)
        for env, want in ((env_a, "session-aaa"), (env_b, "session-bbb")):
            parts = M.split_addr(env["replyTo"])
            self.assertEqual(parts["sessionId"], want)
            self.assertEqual((parts["leg"], parts["hostId"]), ("harbor", "local"))
        # 各自独立成信：不同 msgId
        self.assertNotEqual(env_a["msgId"], env_b["msgId"])

    def test_same_title_conflicts_with_old_binding(self):
        """同标题撞旧绑定：要人确认，不许自己挑一个。"""
        d = R.decide(has_address=True, name_conflict=True, candidates=2)
        self.assertEqual(d["decision"], R.CONFIRM)
        self.assertTrue(d["needs_human"])

    def test_queue_item_survives_stop_and_blocks_new_queues(self):
        """停止后队列项仍在：已有排队就先暂缓；本轮被中断则要人确认。"""
        self.assertEqual(R.decide(pending=1)["decision"], R.DEFER)
        d = R.decide(turn_status="interrupted")
        self.assertEqual(d["decision"], R.CONFIRM)
        self.assertEqual(d["reason"], "last_turn_interrupted")

    def test_same_id_body_landed_but_turn_not_finished(self):
        """同号正文已落盘：同号同文算重复，同号异文拒收。"""
        body = "正文"
        env = M.new_envelope(task_id="t1", msg_id="m-001", from_addr=M.addr("harbor", "local", "session-aaa"))
        text = M.wrap(body, env)
        seen = {}
        first = M.accept_incoming(text, seen)
        self.assertEqual(first["decision"], "accept")
        seen[first["msgId"]] = first["bodySha256"]  # 调用方负责记账（模块只判不写）
        dup = M.accept_incoming(text, seen)
        self.assertEqual(dup["decision"], "duplicate")
        other = M.wrap(body + "（改过）", env)
        conflict = M.accept_incoming(other, seen)
        self.assertEqual(conflict["decision"], "reject")
        self.assertEqual(conflict.get("reason"), "same_id_diff_body")


if __name__ == "__main__":
    unittest.main(verbosity=2)