"""用本仓库判据表验证传球门 CLI。"""
import json
import os
import shutil
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "tools" / "pass_gate.py"
SPEC = REPO / "templates" / "传球判据.json"

CASES = [
    # (说明, 参数, 期望退出码, 期望结论关键词)
    ("红线 → 找用户", ["--risk", "red"], 10, "交出去"),
    ("红线不受其他事实影响", ["--risk", "red", "--evidence", "pointable", "--owner", "mine"], 10, "交出去"),
    ("别人的身体 → 交", ["--owner", "theirs"], 10, "交出去"),
    ("看不见自己（自己重启）→ 交", ["--blind-spot"], 10, "交出去"),
    ("证据指不出 → 交（unresolved）", ["--evidence", "vague"], 10, "unresolved"),
    ("证据完全拿不到 → 交", ["--evidence", "none"], 10, "交出去"),
    ("试了两次 → 交", ["--tries", "2"], 10, "交出去"),
    ("试了一次还不能交", ["--tries", "1", "--evidence", "pointable", "--risk", "green"], 0, "自己做"),
    ("纯本地只读能指认 → 自己做", ["--risk", "green", "--evidence", "pointable", "--tries", "0"], 0, "自己做"),
    ("不填任何参数 → 保守判交", [], 10, "交出去"),
    ("demo 能跑", ["--demo"], 0, "传球门判定"),
]


class PassGateTest(unittest.TestCase):
    def test_release_policy(self):
        policy = json.loads(SPEC.read_text(encoding="utf-8-sig"))
        self.assertTrue(policy.get("规则"))
        self.assertIn("交出去", policy.get("_兜底", ""))

    def test_cli_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "hub"
            policy = root / "10-governance" / "守则" / SPEC.name
            policy.parent.mkdir(parents=True)
            shutil.copyfile(REPO / "templates" / ".archive-root.json", root / ".archive-root.json")
            shutil.copyfile(SPEC, policy)
            env = {**os.environ, "ARCHIVE_ROOT": str(root), "ARCHIVE_ID_EXPECTED": "user-multiagent-hub",
                   "PROJECT_ROOT": temp, "LOCALAPPDATA": temp, "PYTHONIOENCODING": "utf-8"}
            for description, args, want_code, want_keyword in CASES:
                with self.subTest(case=description):
                    result = subprocess.run([sys.executable, str(GATE), *args], env=env,
                                            capture_output=True, text=True, encoding="utf-8")
                    self.assertEqual(result.returncode, want_code, result.stdout + result.stderr)
                    self.assertIn(want_keyword, result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
