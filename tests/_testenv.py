"""测试用沙盒根：给 paths.py 一个独立、不摸宿主机的档案根。"""
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)
import json
import os
import tempfile


def sandbox() -> str:
    root = tempfile.mkdtemp(prefix="mack-test-")
    with open(os.path.join(root, ".archive-root.json"), "w", encoding="utf-8") as fh:
        json.dump({"archive_id": "user-multiagent-hub", "structure_version": "0.4.0",
                   "namespaces": {"config": "00-config", "governance": "10-governance",
                                  "projects": "20-projects", "roles": "30-roles",
                                  "resources": "40-resources", "archive": "90-archive",
                                  "state": "_state", "local": "_local"}}, fh)
    for zone in ("00-config", "10-governance", "20-projects", "30-roles",
                 "40-resources", "90-archive", "_state", "_local"):
        os.makedirs(os.path.join(root, zone), exist_ok=True)
    os.environ["ARCHIVE_ROOT"] = root
    os.environ["LOCALAPPDATA"] = os.path.join(root, "_local")
    return root


if __name__ == "__main__":
    print(sandbox())