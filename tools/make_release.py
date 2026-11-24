#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打一个发布包，并生成 kit-manifest.json（维护者用）。

用法：
    python tools/make_release.py --version 0.3.3
    python tools/make_release.py --version 0.3.3 --out dist --notes "修了什么"

做四件事：
1. 按 git 跟踪清单打包成 kit-<版本>.zip（不把 .git、__pycache__、dist 打进去）；
2. 算 sha256 与字节数；
3. 在 ZIP 外生成 kit-manifest.json（记录版本、字节数与 SHA-256）；
4. 打印下一步：建 tag、传 Release、贴清单。

退出码：0 成功；2 参数或环境不对。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KIT = "multi-ai-collab-kit"
EXCLUDE_DIRS = {".git", "__pycache__", "dist", ".pytest_cache"}
# A ZIP cannot contain a manifest with the SHA-256 of that same ZIP.
MANIFEST_NAME = "kit-manifest.json"


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        raise SystemExit("读不到 git 跟踪清单：%s" % out.stderr.decode("utf-8", "replace").strip())
    files = []
    for name in dict.fromkeys(out.stdout.decode("utf-8").split("\0")):
        relative = Path(name)
        if not name or name == MANIFEST_NAME or EXCLUDE_DIRS.intersection(relative.parts):
            continue
        path = REPO / relative
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"无法打包跟踪文件：{name}")
        files.append(path)
    return files


def build_zip(files: list[Path], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            entry = zipfile.ZipInfo(p.relative_to(REPO).as_posix())
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            zf.writestr(entry, p.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打发布包 + 生成清单")
    ap.add_argument("--version", required=True, help="与 VERSION 文件相同的发布版本")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    ap.add_argument("--notes", default="", help="一句话说明这版改了什么")
    ap.add_argument("--min-dsh", default="0.4.0", help="要求的最低 DSH 版本")
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--manifest-out", default=MANIFEST_NAME, help="外置清单路径（默认仓库根）")
    a = ap.parse_args(argv)

    version = (REPO / "VERSION").read_text(encoding="utf-8-sig").strip()
    if a.version != version or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*", version):
        ap.error(f"--version 应是与 VERSION 一致的发布版本号（当前为 {version}）")
    files = tracked_files()
    if not files:
        print("没有可打包的文件")
        return 2
    out_dir = REPO / a.out
    zip_path = out_dir / ("kit-%s.zip" % a.version)
    manifest_path = REPO / a.manifest_out
    if zip_path.resolve() == manifest_path.resolve():
        ap.error("清单路径不能与 ZIP 路径相同")
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
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    print("打包：", zip_path, "（%d 个文件，%.1f KB）" % (len(files), len(data) / 1024.0))
    print("清单：", manifest_path)
    print("sha256：", manifest["assets"][0]["sha256"])
    print("\n下一步：")
    print("  1) git tag v%s && git push origin v%s" % (a.version, a.version))
    print("  2) 先上传并核对 Release 附件 %s" % zip_path.name)
    print("  3) 如需公布校验清单，可将外置 kit-manifest.json 随 Release 一并发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())