#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把规则正本刷进各客户端的注入层（一段带标记的文本，幂等）。

用法：
    python tools/init_rules.py --target AGENTS.md          # 写入/更新段落
    python tools/init_rules.py --target AGENTS.md --check   # 只校验，不写
    python tools/init_rules.py --target AGENTS.md --remove  # 撤掉段落

规则正本在 rules/core.md；段落用 <!-- MACK-RULES:BEGIN sha256=... --> 包起来，
重复执行只会替换这一段，不会重复追加。

退出码：0 成功/校验一致；3 校验发现漂移；2 参数或正本缺失。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CORE = os.path.join(REPO, "rules", "core.md")
BEGIN_PREFIX = "<!-- MACK-RULES:BEGIN"
END = "<!-- MACK-RULES:END -->"


def load_core() -> tuple[str, str]:
    src = io.open(CORE, encoding="utf-8").read().strip()
    body = src
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return body, digest


def render(body: str, digest: str) -> str:
    return f"{BEGIN_PREFIX} sha256={digest} -->\n{body}\n{END}\n"


def find_block(text: str) -> tuple[int, int] | None:
    start = text.find(BEGIN_PREFIX)
    if start < 0:
        return None
    end = text.find(END, start)
    if end < 0:
        return None
    return start, end + len(END)


def install(target: str, body: str, digest: str) -> int:
    block = render(body, digest)
    text = io.open(target, encoding="utf-8").read() if os.path.exists(target) else ""
    span = find_block(text)
    if span:
        new = text[:span[0]] + block + text[span[1]:]
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    io.open(target, "w", encoding="utf-8", newline="\n").write(new)
    print(f"已写入：{target}（段落 {digest}）")
    return 0


def check(target: str, body: str, digest: str) -> int:
    if not os.path.exists(target):
        print(f"目标不存在：{target}")
        return 3
    text = io.open(target, encoding="utf-8").read()
    span = find_block(text)
    if not span:
        print("目标里还没有规则段落")
        return 3
    block = text[span[0]:span[1]]
    inner = block.split("\n", 1)[1].rsplit(END, 1)[0].strip()
    actual = hashlib.sha256(inner.encode("utf-8")).hexdigest()[:16]
    if actual != digest:
        print("段落与正本不一致（有漂移）")
        return 3
    print("段落与正本一致")
    return 0


def remove(target: str) -> int:
    if not os.path.exists(target):
        print(f"目标不存在：{target}")
        return 2
    text = io.open(target, encoding="utf-8").read()
    span = find_block(text)
    if not span:
        print("没有可撤的段落")
        return 0
    io.open(target, "w", encoding="utf-8", newline="\n").write(text[:span[0]] + text[span[1]:])
    print("已撤掉段落")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把规则正本刷进注入层")
    ap.add_argument("--target", required=True, help="要写入的文件，例如 AGENTS.md")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.exists(CORE):
        print(f"找不到规则正本：{CORE}")
        return 2
    body, digest = load_core()
    if a.remove:
        return remove(a.target)
    if a.check:
        return check(a.target, body, digest)
    return install(a.target, body, digest)


if __name__ == "__main__":
    sys.exit(main())