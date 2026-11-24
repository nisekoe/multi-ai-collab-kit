#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""初始化协作工作根：创建最小运行目录、安装通用模板并写入本机指针。"""
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
    "state": "_state",
    "local": "_local",
}
HERE = Path(__file__).resolve().parent
TEMPLATES = {
    "AGENTS.md": "AGENTS.md",
    "通道.json": "_state/通道.json",
    "cordis.patch.yml": "cordis.patch.yml",
}


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
            raise ValueError("已有工作根结构版本与本工具不兼容")
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
    codex_reg = root / "_state" / "codex_sessions.json"
    legacy_regs = (
        root / "_state" / "window_registry" / "GPT桌面端会话登记.json",
        root / "_state" / "GPT桌面端会话登记.json",
    )
    if not codex_reg.exists() and not any(p.exists() for p in legacy_regs):
        codex_reg.write_text('{"lines": []}\n', encoding="utf-8", newline="\n")

    if existing is None:
        sentinel = {"archive_id": archive_id, "structure_version": "0.4.0", "namespaces": ZONES}
        sentinel_path.write_text(json.dumps(sentinel, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8", newline="\n")

    pointer = paths.user_pointer()
    if pointer.exists() and paths._pointer_root() != root and not force:
        print("已有其他工作根的指针，仍使用原指针。换绑时加 --force。")
    else:
        paths._write_pointer(root, archive_id)

    print(f"工作根 {root}（archive_id={archive_id}）")
    print("通道配置", root / TEMPLATES["通道.json"])
    print("本机指针", pointer)
    command = "mack-init-rules" if __package__ else "python tools/init_rules.py"
    print(f'把规则写入此工作根：{command} --root "{root}"')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="初始化多 Agent 协作工作根")
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
