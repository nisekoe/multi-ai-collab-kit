# -*- coding: utf-8 -*-
r"""整套备份：把"找不回来就完了"的东西打成一个包（2026-11-17 补 GPT 审计尾巴 ⑨）。

判据只有一条：**删掉这些东西以后，能不能从这个包把它整个放回去**。所以包里装的
不是整台机器（node_modules、模型这些重装就能回来，装进去只是把包撑大），而是：
档案库（git 全历史）、tools 代码、三份记忆库、DSH 会话、DSH 存储与配置、凭证。

一致性的做法：先停掉会写文件的定时任务（DSH_SessionDigest），数据库用 SQLite 的
在线备份接口复制（不是直接 copy 文件，WAL 没落盘的页也能拿到），做完再把任务恢复。

产出：备份\全量-<时间戳>.zip，里面有 manifest.json（每个文件的大小与 sha256）、
payload/<标签>/… 和仓库的 git bundle。恢复看 tools\restore_all.py。
SOURCES 默认按维护者环境列（含 AstrBot 记忆库、DSH 会话等）；你的机器上不存在的来源
会自动跳过，只备存在的；要备自己的清单就改 SOURCES。找不到档案根（第一个来源）时不打包。

用法：
    python tools\backup_all.py             # 打包
    python tools\backup_all.py --keep-raw  # 打包并保留解开的目录（调试用）
退出码：0 成功 / 1 失败（含冻结恢复失败）/ 2 环境缺项
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HOME = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
DSH = HOME / ".dsh"
OUT_DIR = ROOT / "备份"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
ZIP_PATH = OUT_DIR / ("全量-%s.zip" % STAMP)

DIGEST_TASK = "DSH_SessionDigest"

import sys as _bsys, os as _bos
_bsys.path.insert(0, _bos.path.dirname(_bos.path.abspath(__file__)))
import archive_paths as ap

# (标签, 源路径, 类型) —— 类型：dir 目录、bundle git 仓库、db SQLite 库、file 单文件
SOURCES = [
    ("档案库仓库", (ROOT / "档案库") if ap.write_mode() == "old" else __import__("pathlib").Path(str(ap.paths.resolve_root(for_write=False)[0])), "bundle"),
    ("tools", ROOT / "tools", "dir"),
    ("记忆库", ROOT / "astrbot" / "data" / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db", "db"),
    ("陪伴库", ROOT / "astrbot" / "data" / "plugin_data" / "astrbot_plugin_private_companion" / "installations" / "main" / "runtime" / "companion-v2.sqlite3", "db"),
    ("记忆检索库", ROOT / "astrbot" / "data" / "plugin_data" / "astrbot_plugin_private_companion" / "installations" / "main" / "runtime" / "memory-search.sqlite3", "db"),
    ("DSH会话", DSH / "sessions", "dir"),
    ("DSH存储", DSH / "storages", "dir"),
    ("DSH集成", DSH / "integrations", "dir"),
    ("DSH配置", DSH / "profiles" / "desktop", "config-dir"),
    ("DSH凭证", DSH / ".credentials.yaml", "file"),
    ("AGENTS", Path(os.path.join(os.path.expanduser("~"), "Documents", "deepseek-harness", "default-workspace")), "workspace-cfg"),
]

# 这些目录里的东西重装/重下就能回来，不进包
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv", "uv", "_tmp_", ".plugin-manager"}
SKIP_EXT = {".pyc", ".log"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Report:
    def __init__(self) -> None:
        self.rows = []
        self.errors = []
        self.files = 0
        self.bytes = 0

    def add(self, label: str, n: int, size: int, note: str = "") -> None:
        self.rows.append({"label": label, "files": n, "bytes": size, "note": note})
        self.files += n
        self.bytes += size
        print("  %-10s %5d 个文件 %8.1f MB %s" % (label, n, size / 1048576, note))

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        print("  ! " + msg)


def freeze() -> str:
    """停掉会写文件的定时任务，返回停之前的原状态（Ready/Disabled/Running/未知）。"""
    if os.name != "nt":
        return "未知"
    try:
        out = subprocess.run(["schtasks", "/query", "/tn", DIGEST_TASK, "/fo", "list"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        text = out.stdout or ""
        prev = "Ready"
        for line in text.splitlines():
            if "状态" in line or "Status" in line:
                prev = line.split(":", 1)[1].strip() if ":" in line else line.strip()
        subprocess.run(["schtasks", "/change", "/tn", DIGEST_TASK, "/disable"],
                       capture_output=True, text=True, timeout=30)
        print("已暂停定时任务 %s（原状态 %s），保证流水不在拷贝中途被写" % (DIGEST_TASK, prev))
        return prev
    except Exception as exc:  # noqa: BLE001
        print("  ! 停任务失败（继续做，但会话流水可能拷到一半的状态）：%s" % exc)
        return "未知"


def thaw(prev: str) -> None:
    if os.name != "nt" or prev in ("Disabled", "已禁用"):
        return
    try:
        r = subprocess.run(["schtasks", "/change", "/tn", DIGEST_TASK, "/enable"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            print("定时任务 %s 已恢复" % DIGEST_TASK)
        else:
            print("  ! 定时任务 %s 不在本机，跳过恢复" % DIGEST_TASK)
    except Exception as exc:  # noqa: BLE001
        print("  ! 恢复定时任务失败（不影响备份包本身）：%s" % exc)


def walk_dir(src: Path, label: str, manifest: list, payload: Path, rep: Report) -> None:
    n = size = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in SKIP_EXT:
                continue
            rel = p.relative_to(src).as_posix()
            try:
                st = p.stat()
            except OSError:
                continue
            dst = payload / label / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            manifest.append({"label": label, "path": rel, "bytes": st.st_size, "sha256": sha256(dst)})
            n += 1
            size += st.st_size
    rep.add(label, n, size)


def copy_files(src: Path, label: str, manifest: list, payload: Path, rep: Report, only: tuple | None = None) -> None:
    items = list(src.iterdir()) if src.is_dir() else [src]
    n = size = 0
    for p in items:
        if not p.is_file():
            continue
        if only is not None and p.suffix.lower() not in only:
            continue
        if p.suffix.lower() in SKIP_EXT:
            continue
        dst = payload / label / p.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        manifest.append({"label": label, "path": p.name, "bytes": p.stat().st_size, "sha256": sha256(dst)})
        n += 1
        size += p.stat().st_size
    rep.add(label, n, size)


def backup_db(src: Path, label: str, manifest: list, payload: Path, rep: Report) -> None:
    dst = payload / label / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(src))
    try:
        out = sqlite3.connect(str(dst))
        with out:
            con.backup(out)
        out.close()
    finally:
        con.close()
    manifest.append({"label": label, "path": src.name, "bytes": dst.stat().st_size, "sha256": sha256(dst)})
    rep.add(label, 1, dst.stat().st_size, "（SQLite 在线备份）")


def git_bundle(src: Path, label: str, manifest: list, payload: Path, rep: Report) -> str:
    dst = payload / label / "repo.bundle"
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "-c", "core.quotepath=false", "bundle", "create", str(dst), "--all"],
                       cwd=str(src), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        rep.fail("git bundle 失败：%s" % (r.stderr or r.stdout).strip()[:200])
        return ""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(src), capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout.strip()
    manifest.append({"label": label, "path": "repo.bundle", "bytes": dst.stat().st_size, "sha256": sha256(dst)})
    rep.add(label, 1, dst.stat().st_size, "（git bundle，HEAD %s）" % head[:8])
    return head


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="整套备份")
    ap.add_argument("--keep-raw", action="store_true", help="保留解开的目录（调试用）")
    args = ap.parse_args(argv)

    missing = [(label, str(p)) for label, p, _ in SOURCES if not Path(p).exists()]
    core_missing = any(label == "档案库仓库" for label, _ in missing)
    if core_missing:
        print("找不到档案根，先跑 init_hub.py；本次不打包")
        return 2
    if missing:
        print("以下来源不存在，本次跳过（不阻止打包）：")
        for label, p in missing:
            print("  - %s：%s" % (label, p))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    staging = OUT_DIR / ("全量-%s.staging" % STAMP)
    payload = staging / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    rep = Report()
    manifest = []
    t0 = time.time()
    print("=== 整套备份 %s ===" % STAMP)

    prev = freeze()
    ok = True
    try:
        for label, src, kind in SOURCES:
            src = Path(src)
            if not src.exists():
                print("[%s] 跳过（不存在）：%s" % (label, src))
                continue
            print("[%s] %s" % (label, src))
            if kind == "bundle":
                head = git_bundle(src, label, manifest, payload, rep)
                if not head:
                    ok = False
                manifest.append({"label": label, "path": "@head", "bytes": 0, "sha256": head or ""})
            elif kind == "db":
                backup_db(src, label, manifest, payload, rep)
            elif kind == "dir":
                walk_dir(src, label, manifest, payload, rep)
            elif kind == "config-dir":
                copy_files(src, label, manifest, payload, rep, only=(".yml", ".yaml", ".json", ".txt", ".md"))
            elif kind == "workspace-cfg":
                copy_files(src, label, manifest, payload, rep, only=(".md",))
            elif kind == "file":
                copy_files(src, label, manifest, payload, rep)
    finally:
        thaw(prev)

    info = {
        "stamp": STAMP,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "host": os.environ.get("COMPUTERNAME") or "",
        "files": len(manifest),
        "bytes": rep.bytes,
        "labels": [x["label"] for x in rep.rows],
        "errors": rep.errors,
        "elapsed_s": round(time.time() - t0, 1),
    }
    (staging / "manifest.json").write_text(json.dumps({"info": info, "files": manifest},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")

    print("打包成 zip …")
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(staging / "manifest.json", "manifest.json")
        for p in payload.rglob("*"):
            if p.is_file():
                z.write(p, "payload/" + p.relative_to(payload).as_posix())
    size = ZIP_PATH.stat().st_size

    if not args.keep_raw:
        shutil.rmtree(staging, ignore_errors=True)
    print("=== 完成 ===")
    print("文件 %d 个｜原始 %.1f MB｜包 %.1f MB｜用时 %.0f 秒"
          % (info["files"], info["bytes"] / 1048576, size / 1048576, time.time() - t0))
    print("包：%s" % ZIP_PATH)
    if rep.errors:
        print("有 %d 处没打成（见上）" % len(rep.errors))
        return 1
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
