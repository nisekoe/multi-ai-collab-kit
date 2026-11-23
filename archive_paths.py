r"""档案库路径映射层（批6消费入口切换；v0.3 六区标准化；评审助手 cutover 审查加固）。

旧相对路径（档案库内）→ 新六区相对路径（档案库根 内），经 paths.py 解析根。
读：新库存在则用新库，否则回旧根（过渡期双读可逆）。
写：受写主开关 write_mode（old/hub，默认 old）控制——Phase A 写工具接开关仍写旧根，
Phase B 翻 hub；hub 模式下未映射旧路径直接拒写（防幽灵目录污染六区）。
通道.json 含凭据禁映（走 paths.secrets_dir）。mirror_target 供 sync 总解析新库。
依据：Strangler Fig＋Single Writer（业界成熟的渐进迁移与单写者模式）。"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths

BS = chr(92)
MAPPING = [
    ("工作台/工作台结果.md", "10-governance/ledger/工作台结果.md"),
    ("工作台/待处理事项.md", "10-governance/ledger/待处理事项.md"),
    ("工作台/坑候选.json", "10-governance/ledger/坑候选.json"),
    ("工作台/踩坑清单.md", "10-governance/ledger/踩坑清单.md"),
    ("工作台/守则", "10-governance/守则"),
    ("工作台/传球判据.json", "10-governance/守则/传球判据.json"),
    ("工作台/鼓励词表.md", "10-governance/守则/鼓励词表.md"),
    ("工作台/传球守则.md", "10-governance/守则/传球守则.md"),
    ("工作台/共同愿景.md", "10-governance/共同愿景.md"),
    ("工作台/一页纸", "10-governance/一页纸"),
    ("工作台/工作框架", "10-governance/工作框架"),
    ("工作台/临时本子-桌面助手.md", "30-roles/desktop_bot/scratch.md"),
    ("工作台/临时本子-飞书助手.md", "30-roles/feishu_bot/scratch.md"),
    ("工作台/飞书助手记忆正本", "30-roles/feishu_bot/memory"),
    ("工作台/身份正本.md", "30-roles/_dsh_shared/身份正本.md"),
    ("工作台/人格正本.md", "30-roles/_dsh_shared/人格正本.md"),
    ("工作台/飞书助手情绪状态.json", "_state/飞书助手情绪状态.json"),
    ("工作台/GPT投递回执.json", "_state/GPT投递回执.json"),
    ("工作台/飞书助手投递回执.json", "_state/飞书助手投递回执.json"),
    ("工作台/桌面助手投递回执.json", "_state/桌面助手投递回执.json"),
    ("工作台/回波回执.json", "_state/回波回执.json"),
    ("工作台/推送节流.json", "_state/推送节流.json"),
    ("工作台/采样器批准意图.json", "_state/采样器批准意图.json"),
    ("工作台/_tmp_/来件关联.jsonl", "_state/来件关联.jsonl"),
    ("档案库/会话归档-流水", "_state/会话归档-流水"),
    ("外部协作/GPT桌面端/会话登记.json", "_state/window_registry/GPT桌面端会话登记.json"),
    ("外部协作/GPT桌面端", "_state/window_registry"),
    ("索引", "_local/cache"),
    ("入口.md", "入口.md"),
    ("档案库", "90-archive"),
]
LEGAL_PREFIXES = ("00-config/", "10-governance/", "20-projects/", "30-roles/",
                  "40-resources/", "90-archive/", "_state/", "_local/")
LEGAL_ROOT_FILES = {"AGENTS.md", "入口.md", "manifest-sha256.txt", ".archive-root.json"}

def old_root() -> Path:
    return Path(paths.__file__).resolve().parent.parent / "档案库"

def _normalize(rel: str) -> str:
    rel = rel.replace(BS, "/")
    while "//" in rel:
        rel = rel.replace("//", "/")
    return rel.lstrip("/")

def map_rel(old_rel: str) -> str:
    old_rel = _normalize(old_rel)
    for old, new in MAPPING:
        if old_rel == old or old_rel.startswith(old + "/"):
            return new + old_rel[len(old):]
    return old_rel

def write_mode() -> str:
    p = paths.user_pointer().parent / "write_mode.txt"
    if p.is_file():
        v = p.read_text(encoding="utf-8-sig").strip().lower()
        if v in ("old", "hub"):
            return v
    return "old"

def mirror_target(old_rel: str) -> Path:
    return paths.resolve(map_rel(old_rel), for_write=True)

def resolve(old_rel: str, for_write: bool = False) -> Path:
    norm = _normalize(old_rel)
    if norm == "工作台/通道.json" or norm.endswith("/通道.json"):
        raise paths.RootError("通道.json 含凭据禁映档案库；走 paths.secrets_dir()")
    new_rel = map_rel(norm)
    if for_write:
        if write_mode() == "old":
            return old_root() / norm.replace("/", os.sep)
        legal = new_rel.startswith(LEGAL_PREFIXES) or new_rel in LEGAL_ROOT_FILES
        if not legal:
            raise paths.RootError("未映射旧路径拒写（防幽灵目录）：%s -> %s" % (norm, new_rel))
        return paths.resolve(new_rel, for_write=True)
    try:
        target = paths.resolve(new_rel, for_write=False)
        if target.exists():
            return target
    except paths.RootError:
        pass
    return old_root() / norm.replace("/", os.sep)

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="旧档案相对路径→新六区解析")
    ap.add_argument("old_rel")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    print("write_mode:", write_mode())
    print("map:", map_rel(a.old_rel))
    try:
        print("resolve:", resolve(a.old_rel, a.write))
    except paths.RootError as exc:
        print("ROOT_ERROR:", exc)
        return 3
    return 0

if __name__ == "__main__":
    raise SystemExit(main())