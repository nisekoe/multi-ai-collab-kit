"""规则监视器测试：正本变了才同步，没变只校验，手改会被报漂移。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(REPO, "tools", "rules_watch.py")
CORE = os.path.join(REPO, "rules", "core.md")
STATE = os.path.join(REPO, "rules", ".sync-state.json")


def run(*args):
    return subprocess.run([sys.executable, WATCH, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


class RulesWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mack-watch-")
        self.target = os.path.join(self.tmp, "AGENTS.md")
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("# 说明\n")
        self.state_backup = None
        if os.path.exists(STATE):
            self.state_backup = open(STATE, encoding="utf-8").read()

    def tearDown(self):
        if self.state_backup is None:
            if os.path.exists(STATE):
                os.remove(STATE)
        else:
            with open(STATE, "w", encoding="utf-8") as fh:
                fh.write(self.state_backup)

    def test_first_sync_then_quiet(self):
        r1 = run("--once", "--target", self.target, "--force")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        self.assertIn("MACK-RULES:BEGIN", open(self.target, encoding="utf-8").read())
        r2 = run("--once", "--target", self.target)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("一致", r2.stdout)

    def test_manual_edit_detected(self):
        run("--once", "--target", self.target, "--force")
        text = open(self.target, encoding="utf-8").read().replace("## 一、档案根", "## 一、档案根（手改）", 1)
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write(text)
        r = run("--once", "--target", self.target)
        self.assertEqual(r.returncode, 3, r.stdout)

    def test_new_target_syncs_even_if_digest_matches(self):
        # 状态里 digest 与正本一致、targets 列的是别的路径（克隆自维护者本机的旧状态）：
        # 新目标也必须写一次，不能只做 --check 把目标永远留在“无规则段落”。
        run("--once", "--target", self.target, "--force")
        state = json.loads(open(STATE, encoding="utf-8").read())
        state["targets"] = [os.path.join(self.tmp, "someone-elses-AGENTS.md")]
        with open(STATE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, ensure_ascii=False))
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("# 说明\n")
        r = run("--once", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("MACK-RULES:BEGIN", open(self.target, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main(verbosity=2)