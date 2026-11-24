"""规则监视器测试：正本变了才同步，没变只校验，手改会被报漂移。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(REPO, "tools", "rules_watch.py")



class RulesWatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mack-watch-")
        self.addCleanup(temp.cleanup)
        self.tmp = temp.name
        self.target = os.path.join(self.tmp, "AGENTS.md")
        self.state = Path(self.tmp) / "sync-state.json"
        Path(self.target).write_text("# 说明\n", encoding="utf-8")

    def run_tool(self, *args):
        env = {**os.environ, "KIT_RULES_STATE": str(self.state)}
        return subprocess.run([sys.executable, WATCH, *args], env=env,
                              capture_output=True, text=True, encoding="utf-8")

    def test_first_sync_then_quiet(self):
        r1 = self.run_tool("--once", "--target", self.target, "--force")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        self.assertIn("MACK-RULES:BEGIN", Path(self.target).read_text(encoding="utf-8"))
        r2 = self.run_tool("--once", "--target", self.target)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("一致", r2.stdout)

    def test_manual_edit_detected(self):
        self.run_tool("--once", "--target", self.target, "--force")
        text = Path(self.target).read_text(encoding="utf-8").replace("## 一、档案根", "## 一、档案根（手改）", 1)
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write(text)
        r = self.run_tool("--once", "--target", self.target)
        self.assertEqual(r.returncode, 3, r.stdout)

    def test_new_target_syncs_even_if_digest_matches(self):
        # 状态里 digest 与正本一致、targets 列的是别的路径（克隆自维护者本机的旧状态）：
        # 新目标也必须写一次，不能只做 --check 把目标永远留在“无规则段落”。
        self.run_tool("--once", "--target", self.target, "--force")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["targets"] = [os.path.join(self.tmp, "someone-elses-AGENTS.md")]
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, ensure_ascii=False))
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("# 说明\n")
        r = self.run_tool("--once", "--target", self.target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("MACK-RULES:BEGIN", Path(self.target).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)