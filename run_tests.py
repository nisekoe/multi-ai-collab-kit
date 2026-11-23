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
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def sandbox() -> str:
    import _testenv
    return _testenv.sandbox()


def main() -> int:
    root = sandbox()
    env = dict(os.environ)
    env["ARCHIVE_ROOT"] = root
    env["LOCALAPPDATA"] = os.path.join(root, "_local")
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    print("沙盒档案根：", root)
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", HERE, "-p", "test_*.py"]
    rc = subprocess.call(cmd, cwd=HERE, env=env)
    if rc == 0:
        print("\n全部通过")
    else:
        print("\n有失败，看上面的输出")
    return rc


if __name__ == "__main__":
    sys.exit(main())