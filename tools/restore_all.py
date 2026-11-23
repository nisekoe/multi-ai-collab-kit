# -*- coding: utf-8 -*-
r"""整套恢复（演练用）：把一个"全量-*.zip"解开、逐个校验、把仓库从 bundle 克隆出来（2026-11-17 补 ⑨）。

它做的是**真的恢复**，不是看一眼包在不在：
  · 解压到指定目录（演练就指到临时目录，不碰在用的数据）；
  · 每个文件按 manifest 的 sha256 逐个核，核完报告几个过几个不过；
  · 三份 SQLite 库打开做 integrity_check，并数一下行数（能打开、没坏页）；
  · 仓库用 git bundle 克隆一份新的，核对 HEAD 与包里记的一致——这条等于证明
    "档案库能被整个重建"，而不只是"文件还在"。

不会覆盖任何在用的东西：只往 --to 指的目录里放。真要覆盖回原位，那是人工另一步。

用法：
    python tools\restore_all.py --from 备份\全量-20261117-200350.zip --to <临时目录>
退出码：0 全部通过 / 1 有不过的项（报告里列出来）
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
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="整套恢复演练")
    ap.add_argument("--from", dest="src", required=True, help="全量-*.zip")
    ap.add_argument("--to", dest="dst", required=True, help="解到哪个目录（会被清空重建）")
    args = ap.parse_args(argv)

    src = Path(args.src)
    if not src.exists():
        print("包不存在：%s" % src)
        return 2
    dst = Path(args.dst)
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    t0 = time.time()
    print("=== 恢复演练：%s ===" % src.name)
    print("解开到：%s" % dst)
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        z.extractall(dst)
    print("包里 %d 个条目｜清单 %d 个文件" % (len(names), len(manifest["files"])))

    ok = 0
    bad = []
    for item in manifest["files"]:
        p = dst / "payload" / item["label"] / item["path"]
        if item["path"] == "@head":
            continue
        if not p.exists():
            bad.append("缺文件 %s/%s" % (item["label"], item["path"]))
            continue
        if p.stat().st_size != item["bytes"] or sha256(p) != item["sha256"]:
            bad.append("内容不一致 %s/%s" % (item["label"], item["path"]))
            continue
        ok += 1
    print("逐个核对：过 %d｜不过 %d" % (ok, len(bad)))

    # SQLite：能不能打开、有没有坏页、行数
    dbs = []
    for item in manifest["files"]:
        if item["label"] in ("记忆库", "陪伴库", "记忆检索库") and item["path"].endswith((".db", ".sqlite3")):
            p = dst / "payload" / item["label"] / item["path"]
            try:
                con = sqlite3.connect(str(p))
                chk = con.execute("PRAGMA integrity_check").fetchone()[0]
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                rows = {}
                for t in tables[:200]:
                    try:
                        rows[t] = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                    except Exception:  # noqa: BLE001
                        pass
                con.close()
                total = sum(rows.values())
                dbs.append((item["label"], chk, len(tables), total))
                print("  %-8s integrity=%s｜表 %d｜行合计 %d" % (item["label"], chk, len(tables), total))
                if chk != "ok":
                    bad.append("%s integrity_check=%s" % (item["label"], chk))
            except Exception as exc:  # noqa: BLE001
                bad.append("%s 打不开：%s" % (item["label"], exc))
                print("  %-8s 打不开：%s" % (item["label"], exc))

    # 仓库：从 bundle 克隆，核对 HEAD
    head = ""
    for item in manifest["files"]:
        if item["label"] == "档案库仓库" and item["path"] == "@head":
            head = item["sha256"]
    bundle = dst / "payload" / "档案库仓库" / "repo.bundle"
    clone_dir = dst / "_clone"
    if bundle.exists():
        r = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(bundle), str(clone_dir)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            bad.append("git 克隆失败：%s" % (r.stderr or "").strip()[:200])
            print("  git 克隆失败：%s" % (r.stderr or "").strip()[:200])
        else:
            got = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(clone_dir), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace").stdout.strip()
            files = subprocess.run(["git", "ls-files"], cwd=str(clone_dir), capture_output=True,
                                   text=True, encoding="utf-8", errors="replace").stdout.splitlines()
            same = got == head
            print("  git 克隆成功：HEAD %s（包里记 %s）%s｜版本库文件 %d 个"
                  % (got[:8], head[:8], "✓" if same else "✗", len(files)))
            if not same:
                bad.append("仓库 HEAD 不一致：克隆 %s / 清单 %s" % (got[:8], head[:8]))
    else:
        bad.append("包里没有 repo.bundle")

    size = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file())
    print("=== 结果 ===")
    print("用时 %.0f 秒｜解开 %.1f MB｜不过 %d 项" % (time.time() - t0, size / 1048576, len(bad)))
    for b in bad[:20]:
        print("  ! " + b)
    if bad:
        return 1
    print("结论：这个包**能整个放回去**（文件、库、仓库都验过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
