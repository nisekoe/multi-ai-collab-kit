"""paths.py 寻根契约测试（F3 一页纸 2026-11-22 验收①）。
覆盖：4 级寻根各优先级／写根绑定拒哨兵副本／相对解析防逃逸／版本与命名空间安全闸／中文空格百分号根／根缺失。
"""
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import os
import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths

if __name__ == "__main__":

    PASS = 0
    FAIL = 0
    BS = chr(92)
    NL = chr(10)


    def check(name, cond):
        global PASS, FAIL
        if cond:
            PASS += 1
            print("PASS", name)
        else:
            FAIL += 1
            print("FAIL", name)


    def make_archive(root, version="0.4.0", ns=None, aid="test-hub"):
        root.mkdir(parents=True, exist_ok=True)
        data = {"archive_id": aid, "structure_version": version,
                "namespaces": ns or {"config": "00-config", "roles": "30-roles"}}
        (root / paths.SENTINEL).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (root / "00-config").mkdir(exist_ok=True)
        (root / "00-config" / "manifest.json").write_text("{}", encoding="utf-8")
        return data


    def expect_error(fn):
        try:
            fn()
            return False
        except paths.RootError:
            return True


    tmp = Path(tempfile.mkdtemp(prefix="paths_test_"))
    orig_env = os.environ.get("ARCHIVE_ROOT")
    orig_la = os.environ.get("LOCALAPPDATA")
    orig_aid = os.environ.get("ARCHIVE_ID_EXPECTED")
    os.environ.pop("ARCHIVE_ROOT", None)
    os.environ["ARCHIVE_ID_EXPECTED"] = "test-hub"
    os.environ["LOCALAPPDATA"] = str(tmp / "la")  # 隔离指针，初始无 root.txt
    try:
        rootA = tmp / "archiveA"
        make_archive(rootA)
        rootB = tmp / "archiveB"
        make_archive(rootB, aid="test-hub")

        r, s, d = paths.resolve_root(cli_root=str(rootA))
        check("cli root", r == rootA.absolute() and s == "cli")

        os.environ["ARCHIVE_ROOT"] = str(rootA)
        r, s, d = paths.resolve_root()
        check("env root", s == "env:ARCHIVE_ROOT" and r == rootA.absolute())
        r, s, d = paths.resolve_root(cli_root=str(rootB))
        check("cli beats env", s == "cli" and r == rootB.absolute())
        del os.environ["ARCHIVE_ROOT"]

        sub = rootA / "00-config"
        r, s, d = paths.resolve_root(cwd=str(sub))
        check("sentinel-up read", s == "sentinel-up" and r == rootA.absolute())
        check("write rejects sentinel-up", expect_error(lambda: paths.resolve_root(cwd=str(sub), for_write=True)))

        ptr = paths.user_pointer()
        ptr.parent.mkdir(parents=True, exist_ok=True)
        ptr.write_text(str(rootA) + NL, encoding="utf-8")
        r, s, d = paths.resolve_root(for_write=True)
        check("pointer write binding", s.startswith("pointer:") and r == rootA.absolute())
        r, s, d = paths.resolve_root(cwd=str(tmp))
        check("pointer read fallback", s.startswith("pointer:"))

        p = paths.resolve("00-config/manifest.json", cli_root=str(rootA))
        check("resolve inside", p == (rootA / "00-config" / "manifest.json").resolve())
        check("resolve dotdot escape blocked", expect_error(lambda: paths.resolve("../escape.txt", cli_root=str(rootA))))
        check("resolve deep escape blocked", expect_error(lambda: paths.resolve("../../windows", cli_root=str(rootA))))

        rootV = tmp / "archiveV"
        make_archive(rootV, version="9.9.9")
        check("version unsupported stops", expect_error(lambda: paths.resolve_root(cli_root=str(rootV))))
        rootN = tmp / "archiveN"
        make_archive(rootN, ns={"config": "C:" + BS + "evil"})
        check("namespace absolute blocked", expect_error(lambda: paths.resolve_root(cli_root=str(rootN))))
        rootE = tmp / "archiveE"
        make_archive(rootE, ns={"config": "../evil"})
        check("namespace escape blocked", expect_error(lambda: paths.resolve_root(cli_root=str(rootE))))
        check("root missing stops", expect_error(lambda: paths.resolve_root(cli_root=str(tmp / "nope"))))

        rootC = tmp / "中文 空格%20库"
        make_archive(rootC)
        r, s, d = paths.resolve_root(cli_root=str(rootC))
        check("chinese space percent root", r == rootC.absolute() and s == "cli")
        p = paths.resolve("00-config/manifest.json", cli_root=str(rootC))
        check("resolve in chinese root", p.exists())

        ptr2 = paths.set_pointer(str(rootC))
        check("set_pointer writes binding", ptr2.is_file() and rootC.name in ptr2.read_text(encoding="utf-8"))
        rootM = tmp / "archiveM"
        make_archive(rootM, aid="wrong-id")
        check("archive_id mismatch stops", expect_error(lambda: paths.resolve_root(cli_root=str(rootM))))
        check("resolve absolute input rejected", expect_error(lambda: paths.resolve("C:" + BS + "evil", cli_root=str(rootA))))
        check("resolve leading slash rejected", expect_error(lambda: paths.resolve("/etc/passwd", cli_root=str(rootA))))
    finally:
        if orig_env is None:
            os.environ.pop("ARCHIVE_ROOT", None)
        else:
            os.environ["ARCHIVE_ROOT"] = orig_env
        if orig_la is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = orig_la
        if orig_aid is None:
            os.environ.pop("ARCHIVE_ID_EXPECTED", None)
        else:
            os.environ["ARCHIVE_ID_EXPECTED"] = orig_aid
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT PASS=%d FAIL=%d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
