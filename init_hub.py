#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""一键初始化：建六区骨架、放模板、绑指针、跑自检、打印要贴给 AI 的段落。

用法：
    python init_hub.py D:\my-hub            # 建到指定目录
    python init_hub.py D:\my-hub --id my-hub-2026

做四件事：
1. 建六区（00-config / 10-governance / 20-projects / 30-roles / 40-resources / 90-archive）＋ _state / _local；
2. 写 .archive-root.json（archive_id、结构版本、命名空间）；
3. 复制 templates/ 里的入口与治理模板（有就复制，没有就生成最小骨架）；
4. 写本机指针（%LOCALAPPDATA%\\agent-archive\\root.txt），跑一次自检，打印注入段落。

依据：2026-11 仓库体检建议；目录约定与 GOVERNANCE.md 一致。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ZONES = {
    "config": "00-config",
    "governance": "10-governance",
    "projects": "20-projects",
    "roles": "30-roles",
    "resources": "40-resources",
    "archive": "90-archive",
    "state": "_state",
    "local": "_local",
}
HERE = Path(__file__).resolve().parent


def create(root: Path, archive_id: str, force: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for zone in ZONES.values():
        (root / zone).mkdir(exist_ok=True)
    sentinel = {"archive_id": archive_id, "structure_version": "0.4.0",
                "namespaces": ZONES,
                "note": "内部仅相对路径；外部寻根：CLI --root > ARCHIVE_ROOT > cwd 向上找哨兵 > 用户级指针"}
    (root / ".archive-root.json").write_text(
        json.dumps(sentinel, ensure_ascii=False, indent=2), encoding="utf-8")

    tpl = HERE / "templates"
    copied = []
    if tpl.is_dir():
        for item in tpl.iterdir():
            if item.is_file():
                shutil.copy2(item, root / item.name)
                copied.append(item.name)
    if not (root / "入口.md").exists():
        (root / "入口.md").write_text(
            "# 入口\n\n这是初始化生成的最小入口页。请按 README 的三步补全：\n"
            "1) 写清本库的用途与边界；2) 写清开工三步；3) 写清新内容放哪个区。\n",
            encoding="utf-8")
    if not (root / "AGENTS.md").exists():
        (root / "AGENTS.md").write_text(
            "# AGENTS\n\n- 档案根：本目录\n- 写文本只走 safe_write.py；写账本只走 ledger_lock.py\n"
            "- 需要人选窗口时走卡片服务，不在正文里甩会话编号\n",
            encoding="utf-8")

    pointer_dir = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "agent-archive"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    target = pointer_dir / "root.txt"
    if force or not target.exists():
        target.write_text(str(root) + "\n", encoding="utf-8")
    else:
        print("已有指针，未覆盖：", target, "（要覆盖加 --force）")
    mode = pointer_dir / "write_mode.txt"
    if force or not mode.exists():
        mode.write_text("hub\n", encoding="utf-8")

    print("已建：", root)
    print("分区：", ", ".join(ZONES.values()))
    print("模板：", ", ".join(copied) if copied else "（无 templates/，已生成最小入口与 AGENTS）")
    print("指针：", pointer_dir / "root.txt")


def check(root: Path) -> bool:
    ok = True
    for zone in ZONES.values():
        if not (root / zone).is_dir():
            print("缺目录：", zone)
            ok = False
    if not (root / ".archive-root.json").is_file():
        print("缺哨兵：.archive-root.json")
        ok = False
    print("自检：", "通过" if ok else "有问题")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一键初始化档案库骨架")
    ap.add_argument("root")
    ap.add_argument("--id", default="user-multiagent-hub")
    ap.add_argument("--force", action="store_true", help="覆盖已有的本机指针")
    a = ap.parse_args(argv)
    root = Path(a.root).expanduser().resolve()
    create(root, a.id, a.force)
    ok = check(root)
    print("\n把下面这段贴进你的 AI 注入层：\n")
    print(f"档案根是 {root}；写文本走 safe_write.py，写账本走 ledger_lock.py；"
          "需要人选窗口时走卡片服务；投递前先核对目标（归档／受保护／工作区漂移／是否正忙）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())