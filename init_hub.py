#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""创建六区档案、安装模板并写入本机指针。"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

if __package__:
    from .tools import paths
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
    import paths

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
TEMPLATES = {
    "入口.md": "入口.md",
    "AGENTS.md": "AGENTS.md",
    "通道.json": "_state/通道.json",
    "传球判据.json": "10-governance/守则/传球判据.json",
    "窗口登记模板.json": "00-config/active_projects.json",
    "预设登记模板.json": "00-config/预设登记.json",
    "cordis.patch.yml": "cordis.patch.yml",
}
STARTER_FILES = (
    "10-governance/ledger/工作台结果.md",
    "10-governance/ledger/待处理事项.md",
    *(f"30-roles/{role}/scratch.md" for role in (
        "desktop_bot", "feishu_bot", "planner_bot", "review_bot", "coder_bot"
    )),
)


def create(root: Path, archive_id: str | None, force: bool = False) -> None:
    templates = HERE / "templates"
    missing = [name for name in TEMPLATES if not (templates / name).is_file()]
    if missing:
        raise ValueError("发行包缺少模板：" + "、".join(missing))

    sentinel_path = root / paths.SENTINEL
    existing = paths._read_sentinel(root) if sentinel_path.exists() else None
    if existing is not None:
        existing_id = existing.get("archive_id")
        if not isinstance(existing_id, str) or not existing_id.strip():
            raise ValueError("已有哨兵缺 archive_id")
        if archive_id is not None and archive_id != existing_id:
            raise ValueError(f"此根已使用 archive_id={existing_id}")
        if existing.get("structure_version") != "0.4.0":
            raise ValueError("已有档案结构版本与本工具不兼容")
        archive_id = existing_id
    else:
        archive_id = archive_id or paths.DEFAULT_ARCHIVE_ID

    root.mkdir(parents=True, exist_ok=True)
    for zone in ZONES.values():
        (root / zone).mkdir(exist_ok=True)
    for name, relative in TEMPLATES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(templates / name, target)
    for relative in STARTER_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.touch()

    if existing is None:
        sentinel = {"archive_id": archive_id, "structure_version": "0.4.0", "namespaces": ZONES}
        sentinel_path.write_text(json.dumps(sentinel, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8", newline="\n")

    pointer = paths.user_pointer()
    if pointer.exists() and paths._pointer_root() != root and not force:
        print("已有其他档案根的指针，仍使用原指针。换绑时加 --force。")
    else:
        paths._write_pointer(root, archive_id)
        mode = pointer.with_name("write_mode.txt")
        if force or not mode.exists():
            mode.write_text("hub\n", encoding="utf-8", newline="\n")

    print(f"档案根 {root}（archive_id={archive_id}）")
    print("通道配置", root / TEMPLATES["通道.json"])
    print("本机指针", pointer)
    command = "mack-init-rules" if __package__ else "python tools/init_rules.py"
    print(f'把规则写入此档案根：{command} --root "{root}"')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="初始化六区档案库")
    parser.add_argument("root")
    parser.add_argument("--id", help="新档案的 ID，已有档案保留原 ID")
    parser.add_argument("--force", action="store_true", help="切换已有本机指针")
    args = parser.parse_args(argv)
    if args.id is not None and not args.id.strip():
        parser.error("--id 不能为空")
    root = Path(args.root).expanduser().resolve()
    try:
        create(root, args.id, args.force)
        return 0
    except (OSError, ValueError, paths.RootError) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
