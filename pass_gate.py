# -*- coding: utf-8 -*-
"""传球门：动手之前，判「这件活该不该交出去」。

和 tools\\approval_gate.py 的分工（两个门，管两件事）：
    approval_gate.py  → 这活「风险高不高」（绿/黄/红）
    pass_gate.py      → 这活「该不该交给别人」

规矩不写在代码里，写在表里：
    工作台/传球判据.json（按 archive_paths 解析）
改规矩＝改那张表的一行，不用动这个脚本。

用法（人可以直接敲，不用记参数顺序）：
    python tools\\pass_gate.py --risk green --owner mine --evidence pointable --tries 0
    python tools\\pass_gate.py --risk red            # 只填一个也行，其余按保守取值
    python tools\\pass_gate.py --demo                # 跑三个例子看效果
    python tools\\pass_gate.py ... --json            # 机器可读

退出码：
    0  = 可以自己做（R6）
    10 = 该交出去（R1~R5 任一命中）
    2  = 参数或表有问题

兜底原则（fail-closed）：表里所有规则都不命中时，也算「交出去」，不默认放行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archive_paths as ap

SPEC = str(ap.resolve("工作台/传球判据.json", for_write=False))
LOG = os.path.join(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "pass_gate_log.jsonl")

# 没填的事实按「最保守」处理：宁可多交一次，也不许漏放一次
DEFAULTS = {"risk": "yellow", "owner": "mine", "blind_spot": False, "evidence": "vague", "tries": 0}

TARGET_BY_OWNER = {"用户": "用户", "身体那一方": "对方（飞书助手或桌面助手，看是谁的身体）",
                   "对方（当见证人）": "对方", "对方": "对方", "—": "—"}


def _match(cond: dict, facts: dict) -> bool:
    """一条规则的全部条件都满足才算命中。条件值可以是单值、候选列表、或 '>=N'。"""
    for key, want in cond.items():
        got = facts.get(key)
        if isinstance(want, str) and want.startswith(">="):
            try:
                if int(got) < int(want[2:]):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if isinstance(want, list):
            if got not in want:
                return False
            continue
        if got != want:
            return False
    return True


def judge(facts: dict, spec: dict) -> dict:
    for rule in spec["规则"]:
        if _match(rule.get("命中", {}), facts):
            return {
                "rule": rule["id"],
                "verdict": rule["结论"],
                "target": rule.get("找谁", "—"),
                "reason": rule.get("理由", ""),
                "action": "pass" if rule["结论"] == "自己做" else "escalate",
            }
    # 兜底：一条都没命中 → 交出去（fail-closed）
    return {"rule": "FALLBACK", "verdict": "交出去（兜底）", "target": "对方",
            "reason": "没有任何规则明确允许自己单独做，按 fail-closed 处理。", "action": "escalate"}


def render(facts: dict, res: dict, spec: dict) -> str:
    L = []
    L.append("=" * 62)
    L.append("传球门判定  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L.append("=" * 62)
    L.append("你填的事实：")
    for k in ("risk", "owner", "blind_spot", "evidence", "tries"):
        L.append("  %-11s = %s" % (k, facts.get(k)))
    L.append("-" * 62)
    L.append("命中规则：%s" % res["rule"])
    L.append("结论    ：%s" % res["verdict"])
    L.append("找谁    ：%s" % res["target"])
    L.append("为什么  ：%s" % res["reason"])
    if res["action"] == "escalate":
        L.append("-" * 62)
        L.append("照这个把请求写出去（六个字段，缺一个就算没交）：")
        for i, f in enumerate(spec["请求草稿"]["字段"], 1):
            L.append("  %d. %s" % (i, f))
        L.append("")
        L.append("写在哪：`工作台\\飞书助手来件.md`（交给她）或 `工作台\\在办看板.md`（记进在办）")
    else:
        L.append("-" * 62)
        L.append("可以做，但必须留痕：动手前在账本写一行，做完附证据（通过标准见守则第六节）。")
    L.append("-" * 62)
    L.append("提醒：" + spec.get("违规提醒", ""))
    L.append("=" * 62)
    return "\n".join(L)


def _log(facts: dict, res: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                "facts": facts, "result": res}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="传球门：判这活该不该交出去")
    ap.add_argument("--risk", choices=["red", "yellow", "green"])
    ap.add_argument("--owner", choices=["mine", "theirs"])
    ap.add_argument("--blind-spot", dest="blind_spot", action="store_true",
                    help="这活里有一段我看不见自己（例如自己重启自己）")
    ap.add_argument("--evidence", choices=["pointable", "vague", "none"])
    ap.add_argument("--tries", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--demo", action="store_true", help="跑三个例子")
    args = ap.parse_args(argv)

    try:
        spec = json.loads(open(SPEC, "rb").read().decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print("判据表读不了：%s\n路径：%s" % (exc, SPEC))
        return 2

    if args.demo:
        cases = [
            ("今晚飞书助手那道题（自己改配置、自己重启、还要读运行时值）",
             {"risk": "yellow", "owner": "mine", "blind_spot": True, "evidence": "vague", "tries": 0}),
            ("要花钱买 key / 动系统设置",
             {"risk": "red", "owner": "mine", "blind_spot": False, "evidence": "pointable", "tries": 0}),
            ("我自己写个只读脚本查配置（能指出证据在哪）",
             {"risk": "green", "owner": "mine", "blind_spot": False, "evidence": "pointable", "tries": 0}),
        ]
        for title, facts in cases:
            print("\n### 例子：" + title)
            res = judge(facts, spec)
            print(render(facts, res, spec))
        return 0

    facts = dict(DEFAULTS)
    for k in ("risk", "owner", "evidence", "tries"):
        v = getattr(args, k, None)
        if v is not None:
            facts[k] = v
    if args.blind_spot:
        facts["blind_spot"] = True

    res = judge(facts, spec)
    _log(facts, res)
    if args.json:
        print(json.dumps({"facts": facts, "result": res}, ensure_ascii=False, indent=2))
    else:
        print(render(facts, res, spec))
    return 10 if res["action"] == "escalate" else 0


if __name__ == "__main__":
    raise SystemExit(main())
