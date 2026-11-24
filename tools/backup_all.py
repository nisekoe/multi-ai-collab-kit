# -*- coding: utf-8 -*-
"""备份档案库、工具与已配置的 DSH 数据，生成可逐文件校验的 ZIP。

KIT_BACKUP_TASK 可填本机任务计划名称。只有查询到可停用的任务且停用成功，
才会在备份结束后恢复它。KIT_BACKUP_WORKSPACE 可指定额外备份 Markdown 的工作区。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HOME = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
DSH = Path(os.environ.get("DSH_HOME") or HOME / ".dsh")
OUT_DIR = ROOT / "备份"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
ZIP_PATH = OUT_DIR / ("全量-%s.zip" % STAMP)

BACKUP_TASK = os.environ.get("KIT_BACKUP_TASK", "").strip()

import sys as _bsys, os as _bos
_bsys.path.insert(0, _bos.path.dirname(_bos.path.abspath(__file__)))
import archive_paths as ap


def backup_sources():
    archive = ap.old_root() if ap.write_mode() == "old" else ap.paths.resolve_root()[0]
    archive_kind = "bundle" if (archive / ".git").exists() else "dir"
    sources = [
        ("档案库仓库", archive, archive_kind),
        ("tools", ROOT / "tools", "dir"),
        ("DSH会话", DSH / "sessions", "dir"),
        ("DSH存储", DSH / "storages", "dir"),
        ("DSH集成", DSH / "integrations", "dir"),
        ("DSH配置", DSH / "profiles" / "desktop", "config-dir"),
        ("DSH凭证", DSH / ".credentials.yaml", "file"),
    ]
    workspace = os.environ.get("KIT_BACKUP_WORKSPACE")
    if workspace:
        sources.append(("AGENTS", Path(workspace), "workspace-cfg"))
    return sources


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


def freeze() -> bool:
    """报告是否成功停用了任务；只有这次停用过才需要恢复。"""
    if os.name != "nt" or not BACKUP_TASK:
        return False
    try:
        status = subprocess.run(["schtasks", "/query", "/tn", BACKUP_TASK, "/fo", "list"],
                                capture_output=True, text=True, errors="replace", timeout=30)
        if status.returncode != 0:
            print(f"计划任务 {BACKUP_TASK} 无法查询，跳过停用。")
            return False
        previous = next((line.partition(":")[2].strip() for line in status.stdout.splitlines()
                         if line.partition(":")[0].strip() in ("Status", "状态")), "")
        if previous not in ("Ready", "就绪", "准备就绪"):
            print(f"计划任务 {BACKUP_TASK} 当前状态为 {previous or '未知'}，跳过停用。")
            return False
        changed = subprocess.run(["schtasks", "/change", "/tn", BACKUP_TASK, "/disable"],
                                 capture_output=True, text=True, errors="replace", timeout=30)
        if changed.returncode != 0:
            print(f"计划任务 {BACKUP_TASK} 停用失败，继续备份。")
            return False
        print(f"已停用计划任务 {BACKUP_TASK}（备份结束后恢复）。")
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"无法停用计划任务 {BACKUP_TASK}：{exc}")
        return False


def thaw(paused: bool) -> None:
    if not paused:
        return
    try:
        result = subprocess.run(["schtasks", "/change", "/tn", BACKUP_TASK, "/enable"],
                                capture_output=True, text=True, errors="replace", timeout=30)
        if result.returncode == 0:
            print(f"定时任务 {BACKUP_TASK} 已恢复。")
        else:
            print(f"定时任务 {BACKUP_TASK} 恢复失败。")
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"无法恢复计划任务 {BACKUP_TASK}：{exc}")


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

    try:
        sources = backup_sources()
    except ap.paths.RootError as exc:
        print(f"找不到档案根：{exc}")
        return 2
    missing = [(label, str(p)) for label, p, _ in sources if not p.exists()]
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

    paused = freeze()
    try:
        for label, src, kind in sources:
            src = Path(src)
            if not src.exists():
                print("[%s] 跳过（不存在）：%s" % (label, src))
                continue
            print("[%s] %s" % (label, src))
            if kind == "bundle":
                head = git_bundle(src, label, manifest, payload, rep)
                manifest.append({"label": label, "path": "@head", "bytes": 0, "sha256": head or ""})
            elif kind == "dir":
                walk_dir(src, label, manifest, payload, rep)
            elif kind == "config-dir":
                copy_files(src, label, manifest, payload, rep, only=(".yml", ".yaml", ".json", ".txt", ".md"))
            elif kind == "workspace-cfg":
                copy_files(src, label, manifest, payload, rep, only=(".md",))
            elif kind == "file":
                copy_files(src, label, manifest, payload, rep)
    finally:
        thaw(paused)

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
