#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打一个发布包，并生成 kit-manifest.json（维护者用）。

用法：
    python tools/make_release.py --version 0.3.0
    python tools/make_release.py --version 0.3.0 --out dist --notes "修了什么"

做四件事：
1. 按 git 跟踪清单打包成 kit-<版本>.zip（不把 .git、__pycache__、dist 打进去）；
2. 算 sha256 与字节数；
3. 生成 kit-manifest.json（字段与 kit_update.py 对齐）；
4. 打印下一步：建 tag、传 Release、贴清单。

退出码：0 成功；2 参数或环境不对。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = "multi-ai-collab-kit"
EXCLUDE_DIRS = {".git", "__pycache__", "dist", ".pytest_cache"}


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise SystemExit("读不到 git 跟踪清单：%s" % (out.stderr or "").strip())
    files = []
    for line in out.stdout.splitlines():
        p = REPO / line.strip()
        if p.is_file() and not any(part in EXCLUDE_DIRS for part in p.parts):
            files.append(p)
    return files


def build_zip(files: list[Path], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(REPO).as_posix())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打发布包 + 生成清单")
    ap.add_argument("--version", required=True, help="如 0.3.0")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    ap.add_argument("--notes", default="", help="一句话说明这版改了什么")
    ap.add_argument("--min-dsh", default="0.4.0", help="要求的最低 DSH 版本")
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--manifest-out", default="kit-manifest.json", help="清单写哪（默认仓库根）")
    a = ap.parse_args(argv)

    files = tracked_files()
    if not files:
        print("没有可打包的文件")
        return 2
    out_dir = REPO / a.out
    zip_path = out_dir / ("kit-%s.zip" % a.version)
    build_zip(files, zip_path)

    data = zip_path.read_bytes()
    manifest = {
        "kit": KIT,
        "version": a.version,
        "channel": a.channel,
        "releasedAt": time.strftime("%Y-%m-%d"),
        "minDsh": a.min_dsh,
        "assets": [{"name": zip_path.name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data)}],
        "plugins": [],
        "notes": a.notes or "见仓库提交记录",
    }
    manifest_path = (REPO / a.manifest_out) if not os.path.isabs(a.manifest_out) else Path(a.manifest_out)
    io.open(manifest_path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    print("打包：", zip_path, "（%d 个文件，%.1f KB）" % (len(files), len(data) / 1024.0))
    print("清单：", manifest_path)
    print("sha256：", manifest["assets"][0]["sha256"])
    print("\n下一步：")
    print("  1) git tag v%s && git push origin v%s" % (a.version, a.version))
    print("  2) 在 Release 里传 %s，并把 kit-manifest.json 正文贴进说明" % zip_path.name)
    print("  3) 合并 kit-manifest.json 到主分支（更新器默认读它）")
    return 0


if __name__ == "__main__":
    sys.exit(main())