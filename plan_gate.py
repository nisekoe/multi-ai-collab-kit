"""开工一页纸（坑库 F3 的闸）。

用法：
    python tools/plan_gate.py start "题目"                  # 生成 工作台/一页纸/<日期>-<题目>.md
    python tools/plan_gate.py approve <file> --by 用户      # 四节非空才写"已批准"
    python tools/plan_gate.py list
    python tools/plan_gate.py audit                         # 查有没有"没批过的新工具"
    python tools/plan_gate.py seed                          # 把当前 tools/*.py 记成台账基线
    python tools/plan_gate.py sync                          # 同上（收工通过后自动调用）

退出码：0 过／1 不过／2 读不了

audit 口径：
- 台账 tools/_已知工具.json 记着上次收工时有哪些工具；不在台账里的 tools/*.py 就是"新工具"；
- test_*.py 不算：那是测试，不是新能力；
- 每个新工具都要有一页纸，状态=已批准、批准人非空、范围那一行点了它的名字。

为什么用台账而不是 git：本项目的 git 仓库根是 档案库，tools/ 不在里面；
新增工具只能靠自己的台账记，不能假装 git 看得见。

边界（诚实）：
- 先后顺序机器判不了。闸只能保证"收工前有一页纸"，保证不了"开工前写的"。
- 台账本身也能被手改——跟清单条数一样，改了就自欺，查的是"有没有记"，不是"有没有良心"。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

TOOLS = os.path.dirname(os.path.abspath(__file__))
import archive_paths  # noqa: E402
PLANS = str(archive_paths.resolve("工作台/一页纸", for_write=True))
BASELINE = os.path.join(TOOLS, "_已知工具.json")
SECTIONS = ("## 要做什么", "## 不做什么", "## 怎么验收", "## 依据")
SKELETON = """# 一页纸：{title}

- 日期：{date}
- 批准人：（待填）
- 范围：（改哪个文件／哪一块）
- 状态：未批准

## 要做什么

（一句话，不许写"优化一下"）

## 不做什么

（边界：哪些不做，为什么）

## 怎么验收

（可执行的判据：命令、输出、证据）

## 依据

（搜了什么／没搜到什么／自拟）
"""


def slugify(title):
    return (re.sub(r"[\\/:*?\"<>|\s]+", "-", title.strip()).strip("-")[:40]) or "未命名"


def section_body(text, heading):
    idx = text.find(heading)
    if idx < 0:
        return None
    rest = text[idx + len(heading):]
    end = len(rest)
    for other in SECTIONS:
        j = rest.find("\n" + other)
        if 0 <= j < end:
            end = j
    return rest[:end]


def empty_sections(text):
    empty = []
    for heading in SECTIONS:
        body = section_body(text, heading)
        if body is None:
            empty.append(heading + "（整节缺）")
            continue
        filled = [ln.strip() for ln in body.splitlines()
                  if ln.strip() and not ln.strip().startswith("（")]
        if not filled:
            empty.append(heading)
    return empty


def current_tools(tools_dir=TOOLS):
    if not os.path.isdir(tools_dir):
        return None
    return sorted(
        name for name in os.listdir(tools_dir)
        if name.endswith(".py") and not name.startswith("test_")
    )


def load_baseline(path=BASELINE):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    names = doc.get("tools")
    return set(names) if isinstance(names, list) else None


def save_baseline(names, path=BASELINE):
    import safe_write
    safe_write.write_text(path, json.dumps(
        {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "tools": sorted(names)},
        ensure_ascii=False, indent=2) + "\n")


def new_tools(tools_dir=TOOLS, baseline_path=BASELINE):
    now = current_tools(tools_dir)
    base = load_baseline(baseline_path)
    if now is None or base is None:
        return None
    return [name for name in now if name not in base]


def approved_plans(plans=PLANS):
    out = []
    if not os.path.isdir(plans):
        return out
    for name in sorted(os.listdir(plans)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(plans, name)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if "状态：已批准" in text and re.search(r"批准人：(?!（)", text):
            out.append((path, text))
    return out


def covers(text, filename):
    return any(line.strip().startswith("- 范围") and filename in line
               for line in text.splitlines())


def cmd_start(title):
    os.makedirs(PLANS, exist_ok=True)
    path = os.path.join(PLANS, "%s-%s.md" % (datetime.now().strftime("%Y-%m-%d"), slugify(title)))
    if os.path.exists(path):
        print("已经有一页纸了，不覆盖：%s" % path)
        return 1
    import safe_write
    safe_write.write_text(path, SKELETON.format(title=title, date=datetime.now().strftime("%Y-%m-%d")))
    print("已生成：%s" % path)
    print("填完四节，再跑：python tools/plan_gate.py approve \"%s\" --by 用户" % path)
    return 0


def cmd_approve(path, by):
    if not os.path.exists(path):
        print("没有这个文件：%s" % path)
        return 1
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    empty = empty_sections(text)
    if empty:
        print("四节还没填满，不批准：")
        for item in empty:
            print("  " + item)
        return 1
    out, seen_state, seen_by = [], False, False
    for line in text.splitlines():
        if line.startswith("- 状态"):
            out.append("- 状态：已批准")
            seen_state = True
        elif line.startswith("- 批准人"):
            out.append("- 批准人：%s（%s）" % (by, datetime.now().strftime("%Y-%m-%d %H:%M")))
            seen_by = True
        else:
            out.append(line)
    if not seen_state:
        out.append("- 状态：已批准")
    if not seen_by:
        out.append("- 批准人：%s" % by)
    import safe_write
    safe_write.write_text(path, "\n".join(out) + "\n")
    print("已批准：%s" % path)
    return 0


def cmd_audit(tools_dir=TOOLS, baseline_path=BASELINE, plans=PLANS):
    now = current_tools(tools_dir)
    base = load_baseline(baseline_path)
    if now is None or base is None:
        print("读不了台账或目录：%s ／ %s" % (baseline_path, tools_dir))
        print("第一次用先跑：python tools/plan_gate.py seed")
        return 2
    fresh = [name for name in now if name not in base]
    if not fresh:
        print("没有新工具（tools 下 %d 个，台账 %d 个）。" % (len(now), len(base)))
        return 0
    plans_text = approved_plans(plans)
    missing = [name for name in fresh if not any(covers(text, name) for _, text in plans_text)]
    if not missing:
        print("新工具 %d 个，都有一页纸点名：%s" % (len(fresh), "、".join(fresh)))
        return 0
    print("这些新工具没有批过的一页纸：")
    for name in missing:
        print("  tools/" + name)
    print("先跑：python tools/plan_gate.py start \"要做什么\" → 填四节 → approve --by 用户")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="one-page plan gate")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("start"); s.add_argument("title")
    a = sub.add_parser("approve"); a.add_argument("file"); a.add_argument("--by", required=True)
    sub.add_parser("list")
    sub.add_parser("audit")
    sub.add_parser("seed")
    sub.add_parser("sync")
    args = ap.parse_args(argv)
    if args.cmd == "start":
        return cmd_start(args.title)
    if args.cmd == "approve":
        return cmd_approve(args.file, args.by)
    if args.cmd in ("seed", "sync"):
        now = current_tools()
        if now is None:
            print("找不到 tools 目录：%s" % TOOLS)
            return 2
        save_baseline(now)
        print("台账已记：%d 个工具。" % len(now))
        return 0
    if args.cmd == "list":
        plans = approved_plans()
        if not plans:
            print("还没有已批准的一页纸。")
            return 1
        for path, _ in plans:
            print("  " + os.path.basename(path))
        return 0
    if args.cmd == "audit":
        return cmd_audit()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
