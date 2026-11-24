"""规则安装器测试：写入幂等、校验能发现漂移、撤销只删段落（unittest 版）。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "tools", "init_rules.py")


def run(*args):
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


class InitRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mack-rules-")
        self.target = os.path.join(self.tmp, "AGENTS.md")
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("# 我的工作说明\n\n原有内容保留。\n")

    def read(self):
        return Path(self.target).read_text(encoding="utf-8")

    def test_install_idempotent(self):
        self.assertEqual(run("--target", self.target).returncode, 0)
        self.assertEqual(run("--target", self.target).returncode, 0)
        text = self.read()
        self.assertEqual(text.count("MACK-RULES:BEGIN"), 1)
        self.assertIn("原有内容保留", text)

    def test_check_and_drift(self):
        run("--target", self.target)
        self.assertEqual(run("--target", self.target, "--check").returncode, 0)
        text = self.read().replace("## 一、档案根", "## 一、档案根（手改）", 1)
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertEqual(run("--target", self.target, "--check").returncode, 3)

    def test_remove_keeps_other_content(self):
        run("--target", self.target)
        self.assertEqual(run("--target", self.target, "--remove").returncode, 0)
        text = self.read()
        self.assertNotIn("MACK-RULES:BEGIN", text)
        self.assertIn("原有内容保留", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)