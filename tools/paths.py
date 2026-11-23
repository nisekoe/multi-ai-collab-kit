"""档案库单一路径解析入口（v0.3 §2.1 寻根契约；F3 一页纸 2026-11-22 用户批准）。

外部 4 级寻根链 ＋ 内部零绝对路径 ＋ 写根本机绑定 ＋ 安全闸。
读取优先级：CLI --root ＞ env ARCHIVE_ROOT ＞ 从 cwd 向上找哨兵 ＞ 用户级单行指针。
写根（for_write）：只认 CLI／env／指针（本机绑定），哨兵向上发现只供读取——
防恢复副本带同一 archive_id 时从副本 cwd 命中哨兵而写错根（GPT 复审补）。
安全闸：根不在／哨兵缺失／版本不支持／命名空间含绝对路径或逃逸 → 报错停写。

依据：12factor.net/config、Windows Known Folder IDs、XDG Base Dir、platformdirs、
Cargo Workspaces；寻根链适配／写根绑定／相对解析为自拟适配（v0.3 三方复审过）。
本机先例：channel_params.py（模块位置推导部署根＋DSH_CHANNEL_FILE 覆盖）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SENTINEL = ".archive-root.json"
SUPPORTED_VERSIONS = {"0.4.0"}
DEFAULT_ARCHIVE_ID = "user-multiagent-hub"  # 期望库 ID（env ARCHIVE_ID_EXPECTED 可覆盖；ID 不符停写）


class RootError(Exception):
    """寻根／校验失败（调用方应停止写入，绝不悄悄另建库）。"""


def user_pointer() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "agent-archive" / "root.txt"


def secrets_dir() -> Path:
    """库外凭据存储（本机专属，不入档案库／不打包／不上公网）。env ARCHIVE_SECRETS_DIR 可覆盖。"""
    override = os.environ.get("ARCHIVE_SECRETS_DIR")
    if override:
        return Path(override).expanduser().absolute()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "agent-archive" / "secrets"


def _read_sentinel(root: Path) -> dict:
    p = root / SENTINEL
    if not p.is_file():
        raise RootError("哨兵缺失：%s" % p)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise RootError("哨兵不可读：%s（%s）" % (p, exc))
    if not isinstance(data, dict):
        raise RootError("哨兵结构非法：%s" % p)
    return data


def _is_relative_ns(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(("/", "\\")) or ":" in value:
        return False
    parts = value.replace("\\", "/").split("/")
    return ".." not in parts


def _validate(root: Path, data: dict) -> None:
    if not root.exists():
        raise RootError("盘符／根不在：%s" % root)
    aid = data.get("archive_id")
    if not isinstance(aid, str) or not aid.strip():
        raise RootError("哨兵缺 archive_id：%s" % root)
    expected = os.environ.get("ARCHIVE_ID_EXPECTED", DEFAULT_ARCHIVE_ID)
    if aid != expected:
        raise RootError("archive_id 不符：%s（期望 %s）停写" % (aid, expected))
    ver = data.get("structure_version")
    if ver not in SUPPORTED_VERSIONS:
        raise RootError("结构版本不支持：%s（支持 %s）" % (ver, sorted(SUPPORTED_VERSIONS)))
    ns = data.get("namespaces") or {}
    for key, val in ns.items():
        if not _is_relative_ns(val):
            raise RootError("命名空间须库内相对路径：%s=%s" % (key, val))


def _find_up_sentinel(cwd: Path):
    cur = cwd.resolve()
    for d in [cur, *cur.parents]:
        if (d / SENTINEL).is_file():
            return d
    return None


def _pointer_root():
    ptr = user_pointer()
    if not ptr.is_file():
        return None
    lines = ptr.read_text(encoding="utf-8-sig").strip().splitlines()
    if not lines or not lines[0].strip():
        return None
    return Path(lines[0].strip()).expanduser().absolute()


def resolve_root(cli_root=None, cwd=None, for_write=False):
    """返回 (root, source, sentinel_data)。写根只认本机绑定，哨兵发现只供读取。"""
    cwd = Path(cwd or os.getcwd())
    if cli_root:
        root = Path(cli_root).expanduser().absolute()
        data = _read_sentinel(root)
        _validate(root, data)
        return root, "cli", data
    env = os.environ.get("ARCHIVE_ROOT")
    if env:
        root = Path(env).expanduser().absolute()
        data = _read_sentinel(root)
        _validate(root, data)
        return root, "env:ARCHIVE_ROOT", data
    if for_write:
        pr = _pointer_root()
        if pr is not None:
            data = _read_sentinel(pr)
            _validate(pr, data)
            return pr, "pointer:%s" % user_pointer(), data
        raise RootError("写根须本机绑定（ARCHIVE_ROOT 或指针 %s）；哨兵发现只供读取" % user_pointer())
    found = _find_up_sentinel(cwd)
    if found is not None:
        data = _read_sentinel(found)
        _validate(found, data)
        return found, "sentinel-up", data
    pr = _pointer_root()
    if pr is not None:
        data = _read_sentinel(pr)
        _validate(pr, data)
        return pr, "pointer:%s" % user_pointer(), data
    raise RootError("寻根失败：CLI／env／哨兵／指针皆无（cwd=%s）" % cwd)


def resolve(rel: str, cli_root=None, cwd=None, for_write=False) -> Path:
    """相对路径解析到库内，防 ../ 逃逸出库；拒绝对路径入参。"""
    if Path(rel).is_absolute() or rel.startswith(("/", chr(92))) or ":" in rel:
        raise RootError("resolve 只接受库内相对路径，拒绝绝对入参：%s" % rel)
    root, _src, _data = resolve_root(cli_root, cwd, for_write)
    target = (root / rel.replace("/", os.sep)).resolve()
    root_r = root.resolve()
    if target != root_r and root_r not in target.parents:
        raise RootError("路径逃逸出库：%s" % rel)
    return target


def set_pointer(root_str: str) -> Path:
    root = Path(root_str).expanduser().absolute()
    data = _read_sentinel(root)
    _validate(root, data)
    ptr = user_pointer()
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text(str(root) + "\n", encoding="utf-8", newline="\n")
    return ptr


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="档案库路径解析（输出实际根＋来源）")
    ap.add_argument("--root", help="CLI 显式根（最高优先）")
    ap.add_argument("--cwd", help="模拟 cwd（哨兵向上发现用）")
    ap.add_argument("--write", action="store_true", help="写根模式（须本机绑定）")
    ap.add_argument("--resolve", help="解析相对路径到库内")
    ap.add_argument("--set-pointer", help="把根写入本机指针（切换写根）")
    a = ap.parse_args(argv)
    try:
        if a.set_pointer:
            ptr = set_pointer(a.set_pointer)
            print("指针已设：%s" % ptr)
            return 0
        if a.resolve:
            print(resolve(a.resolve, a.root, a.cwd, a.write))
            return 0
        root, src, data = resolve_root(a.root, a.cwd, a.write)
        print(json.dumps({"root": str(root), "source": src,
                          "archive_id": data.get("archive_id"),
                          "structure_version": data.get("structure_version")}, ensure_ascii=False))
        return 0
    except RootError as exc:
        print("ROOT_ERROR: %s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
