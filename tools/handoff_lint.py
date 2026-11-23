#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""handoff_lint.py —— 交接四字段的 lint（内部守则（略） 的强制入口）

它解决什么：
  内部守则（略） 写着「缺一字段，接手方可打回」。**但打回靠对方发现** —— 对方没发现，缺字段的交接就滑过去了。
  这个小工具把"打回"变成**发出去之前自己先被提醒一次**。

查什么（照 内部守则（略））：
  1. 四个字段在不在：trace_id / done / next / evidence
  2. evidence 里有没有「依据」二字（缺了跟缺字段一样）
  3. done 里不许出现「我觉得 / 应该 / 大概 / 可能吧」（内部守则（略））
  4. next 里不许出现「你看着办 / 继续推进 / 看着办」（内部守则（略））

用法：
  python tools\\handoff_lint.py --file <交接正文文件>
  python tools\\handoff_lint.py --text "trace_id：…\\ndone：…"
  python tools\\handoff_lint.py --demo          # 看一好一坏两个例子

退出码：
  0 = 四字段齐全且无违规
  1 = 字段齐全但有内容违规（禁用词/空字段/缺依据）
  2 = 缺四字段（结构性门禁，P1-1）或参数/文件问题
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FIELDS = ("trace_id", "done", "next", "evidence")

# 字段行：支持 `trace_id：` / `trace_id:` / `**trace_id**：` / 行首空格
FIELD_RE = {
    f: re.compile(r"(?m)^\s*(?:\*\*)?" + f + r"(?:\*\*)?[ \t]*[：:][ \t]*(.*)$")
    for f in FIELDS
}

# 禁用词（done 字段不许含糊）
BANNED_DONE = ("我觉得", "应该", "大概", "可能吧")
BANNED_NEXT = ("你看着办", "继续推进", "看着办", "再说吧")

# done 的"可核"信号：至少出现一种
EVIDENCE_HINTS = ("字节", "字", "sha", "行", "exit", "退出码", "路径",
                  "条", "行号", "日志", "mtime", "次")


def lint(text: str) -> tuple[list[str], list[str]]:
    """返回 (硬问题, 提示)。"""
    hard, soft = [], []
    got = {}
    for f in FIELDS:
        m = FIELD_RE[f].search(text)
        if not m:
            hard.append("缺字段 `%s`" % f)
        else:
            got[f] = (m.group(1) or "").strip()

    if "evidence" in got:
        ev = got["evidence"]
        if not ev:
            hard.append("`evidence` 是空的")
        elif "依据" not in ev:
            hard.append("`evidence` 里没有「依据」（内部守则（略））")

    if "done" in got:
        d = got["done"]
        if not d:
            hard.append("`done` 是空的")
        else:
            for w in BANNED_DONE:
                if w in d:
                    hard.append("`done` 里出现「%s」（内部守则（略））" % w)
            if not any(h in d for h in EVIDENCE_HINTS):
                soft.append("`done` 里没看到可核的东西（字节/sha/行号/路径/退出码任一）")

    if "next" in got:
        n = got["next"]
        if not n:
            hard.append("`next` 是空的")
        else:
            # 去重：长词先报，短词若被更长的词包含就不再重复报（"你看着办"里含"看着办"）
            hit = [w for w in sorted(BANNED_NEXT, key=len, reverse=True) if w in n]
            kept = [w for w in hit if not any(w != k and w in k for k in hit)]
            for w in kept:
                hard.append("`next` 里出现「%s」（内部守则（略））" % w)

    if "trace_id" in got:
        t = got["trace_id"]
        if t and not re.search(r"第?\s*\d+\s*步|\d", t):
            soft.append("`trace_id` 里没看到步数（应形如「第N步-接手方-日期」）")

    return hard, soft


def missing_fields(text: str) -> list[str]:
    """P1-1 门禁：四字段哪些整行缺失（结构性缺 → 退出码 2，区别于内容违规的 1）。"""
    return [f for f in FIELDS if not FIELD_RE[f].search(text)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="交接四字段 lint（内部守则（略））")
    ap.add_argument("--file", help="交接正文文件（UTF-8）")
    ap.add_argument("--text", help="直接给正文")
    ap.add_argument("--demo", action="store_true", help="看一好一坏两个例子")
    args = ap.parse_args(argv)

    if args.demo:
        good = ("trace_id：第2步-飞书助手-20261111\n"
                "done：配置已改（28485 字节零差、sha 66ed→38c5，备份 .bak-typo3）\n"
                "next：桌面助手去查 run.log 19:30–19:40 的启动行，告诉我重启了几次、几点\n"
                "evidence：依据：自拟；实测备份文件存在、json 合法")
        bad = ("trace_id：第几步来着\n"
               "done：应该处理好了\n"
               "next：你看着办\n"
               "evidence：没问题")
        for label, body in (("✅ 合格的样子", good), ("❌ 会打回的样子", bad)):
            h, s = lint(body)
            print("%s → %s" % (label, "通过" if not h else "不通过"))
            for x in h:
                print("   ✗ " + x)
            for x in s:
                print("   · " + x)
            print()
        return 0

    text = args.text or ""
    if args.file:
        if not os.path.exists(args.file):
            print("文件不存在：%s" % args.file)
            return 2
        text = open(args.file, encoding="utf-8-sig").read()
    if not text.strip():
        print("没有正文：给 --file 或 --text")
        return 2

    hard, soft = lint(text)
    missing = missing_fields(text)
    print("=== 交接 lint（内部守则（略））===")
    if missing:
        print("门禁拦截：缺字段 %s（结构性缺，退出码 2）" % "、".join(missing))
    if not hard:
        print("四字段齐全，没有违规。")
    else:
        print("硬问题：%d 项" % len(hard))
        for x in hard:
            print("  ✗ " + x)
    if soft:
        print("提示：%d 项" % len(soft))
        for x in soft:
            print("  · " + x)
    if missing:
        print("结论：缺字段，门禁打回（退出码 2）")
        return 2
    print("结论：%s" % ("合格" if not hard else "会被打回，改完再发"))
    return 0 if not hard else 1


if __name__ == "__main__":
    raise SystemExit(main())