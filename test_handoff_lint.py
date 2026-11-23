#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_handoff_lint.py —— 交接四字段 lint 的自测（含反向用例）

锁住什么：
  内部守则（略） 的四字段（trace_id/done/next/evidence）+ 三条硬约束，必须**真能判出来**。
  尤其是「缺了会滑过去」这一点：如果 lint 自己漏判，那它就等于没有。

跑法：python tools\\test_handoff_lint.py      退出码 0 全过 / 1 有失败
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("hl", os.path.join(HERE, "handoff_lint.py"))
hl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hl)

fails, cnt = [], [0]


def ck(name, got, want):
    cnt[0] += 1
    if got != want:
        fails.append(name)
    print("  %s %-46s got=%-6s want=%s" % ("OK " if got == want else "FAIL", name, got, want))


GOOD = """trace_id：第2步-飞书助手-20261111
done：配置已改（28485 字节零差、sha 66ed→38c5，备份 .bak-typo3）
next：桌面助手去查 run.log 19:30–19:40 的启动行，告诉我重启了几次、几点
evidence：依据：自拟；实测备份文件存在、json 合法"""

print("=== 一、合格的样子必须过 ===")
h, s = hl.lint(GOOD)
ck("完整四字段 → 没有硬问题", len(h), 0)

print()
print("=== 二、缺字段必须报（反向：缺了要能发现）===")
for f in ("trace_id", "done", "next", "evidence"):
    body = "\n".join(l for l in GOOD.splitlines() if not l.startswith(f))
    h2, _ = hl.lint(body)
    ck("去掉 %s → 报缺字段" % f, any(f in x for x in h2), True)

print()
print("=== 三、三条硬约束必须真拦 ===")
h3, _ = hl.lint(GOOD.replace("evidence：依据：自拟；", "evidence：没问题；"))
ck("evidence 没「依据」→ 拦", any("依据" in x for x in h3), True)

h4, _ = hl.lint(GOOD.replace("配置已改（28485 字节零差", "应该没问题（28485 字节零差"))
ck("done 含「应该」→ 拦", any("应该" in x for x in h4), True)

h5, _ = hl.lint(GOOD.replace("桌面助手去查 run.log", "你看着办 run.log"))
ck("next 含「你看着办」→ 拦", any("你看着办" in x for x in h5), True)
ck("★同一个问题只报一次（不重复报「看着办」）",
   len([x for x in h5 if "看着办" in x]), 1)

print()
print("=== 四、格式容错（全角/半角/加粗/行首空格）===")
for label, body in (
    ("半角冒号", GOOD.replace("：", ":", 1)),
    ("加粗字段名", GOOD.replace("trace_id：", "**trace_id**：", 1)),
    ("行首两空格", GOOD.replace("done：", "  done：", 1)),
):
    hx, _ = hl.lint(body)
    ck("%s → 仍能认出来" % label, len(hx), 0)

print()
print("=== 五、提示项不该变成硬错 ===")
h6, s6 = hl.lint("trace_id：x\ndone：改完了\nnext：去读 A 文件第 3 行\nevidence：依据：自拟")
ck("done 不可核 → 只提示不判错", len(h6), 0)
ck("上面那条确实出了提示", len(s6) >= 1, True)

print()
print("=== 六、P1-1 门禁：缺字段 → missing_fields + 退出码 2 ===")
ck("完整四字段 → missing_fields 空", hl.missing_fields(GOOD), [])
for f in ("trace_id", "done", "next", "evidence"):
    body = "\n".join(l for l in GOOD.splitlines() if not l.startswith(f))
    ck("去掉 %s → missing_fields 含它" % f, f in hl.missing_fields(body), True)
ck("CLI 合格 → 0", hl.main(["--text", GOOD]), 0)
ck("CLI 缺字段 → 2", hl.main(["--text", "done：改完了（3 字节）\nnext：去读 A 第 3 行\nevidence：依据：自拟"]), 2)
ck("CLI 内容违规（缺依据）→ 1", hl.main(["--text", GOOD.replace("evidence：依据：自拟；", "evidence：没问题；")]), 1)

print()
if fails:
    print("FAILED %d 项：%s" % (len(fails), fails))
    raise SystemExit(1)
print("全过：%d 项断言" % cnt[0])