#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令跑完全部离线测试（干净克隆即可用）。

做法：给 paths.py 一个临时沙盒档案根（不摸宿主机的真实档案库），再跑 unittest discover。
依赖宿主状态的用例（hostcheck_*.py）默认跳过，设 MACK_HOST_TESTS=1 才跑。

用法：python run_tests.py
退出码：0 全过；1 有失败。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))


def sandbox() -> str:
    tests = os.path.join(HERE, "tests")
    if tests not in sys.path:
        sys.path.insert(0, tests)
    import _testenv
    return _testenv.sandbox()


def main() -> int:
    root = sandbox()
    env = dict(os.environ)
    env["ARCHIVE_ROOT"] = root
    env["LOCALAPPDATA"] = os.path.join(root, "_local")
    env["PYTHONPATH"] = os.pathsep.join([HERE, os.path.join(HERE, "tools"), env.get("PYTHONPATH", "")])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    print("沙盒档案根：", root)
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", os.path.join(HERE, "tests"), "-p", "test_*.py"]
    rc = subprocess.call(cmd, cwd=HERE, env=env)
    if rc == 0:
        for name in (
            "test_paths.py",
            "test_safe_write.py",
            "test_msg_envelope.py",
            "test_delivery_receipt.py",
            "test_channel_params.py",
            "test_picker.py",
            "test_grouped_card.py",
            "test_window_picker.py",
        ):
            script = Path(HERE) / "tests" / name
            if script.is_file():
                print(f"\n--- 运行独立脚本测试：{name} ---")
                rc = subprocess.call([sys.executable, str(script)], cwd=HERE, env=env)
                if rc:
                    break
    if rc == 0 and os.environ.get("MACK_HOST_TESTS") == "1":
        for script in sorted((Path(HERE) / "tests").glob("hostcheck_*.py")):
            rc = subprocess.call([sys.executable, str(script)], cwd=HERE, env=env)
            if rc:
                break
    if rc == 0:
        print("\n全部通过")
    else:
        print("\n有失败，看上面的输出")
    return rc


if __name__ == "__main__":
    sys.exit(main())