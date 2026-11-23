
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_comm_gate.py —— comm_gate 的分类与判定自测（含反向用例）

为什么要有它（2026-11-11 的教训）：
  **一个从来没拦下过东西的检查工具，可能只是没在检查。**
  所以这里既测"对的能过"，也测"错的必须被判成错的"。

它已经抓出过一个真缺陷（2026-11-11 22:1x）：
  第一版 decide() 里 WAIT 的条件是"距我上轮结束 > 30 秒"，而 SILENT 排在它前面，
  于是**只要 2 条全牢骚就永远判 SILENT，WAIT 一次也走不到 = 死代码**。
  反向用例 "久未开工 + 堆2条 → WAIT" 直接把它照出来了。

跑法（哪个解释器都行，纯函数不依赖 zstandard）：
  python tools\\test_comm_gate.py
退出码：0 全过 / 1 有失败
"""
from __future__ import annotations
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import comm_gate as G  # noqa: E402

if __name__ == "__main__":

    fails = []
    count = [0]


    def ck(name: str, got, want):
        count[0] += 1
        ok = got == want
        print("  %s %-44s got=%-8s want=%s" % ("OK " if ok else "FAIL", name, got, want))
        if not ok:
            fails.append(name)


    NOW = 1_000_000_000_000  # 假装"现在"


    def mk(cls: str, ago_s: int, seq: int = 1) -> dict:
        """造一条消息，ago_s = 距今多少秒。"""
        return {"seq": seq, "time": NOW - ago_s * 1000, "kind": "user",
                "target": "next-turn", "text": "x", "cls": cls}


    print("=== 一、分类（有标记）")
    ck("【意见】", G.classify("【意见】L135 自相矛盾，改掉那个 14:15"), "意见")
    ck("【牢骚】", G.classify("【牢骚】我又搞错了，收回上一条"), "牢骚")
    ck("【继续】", G.classify("【继续】我找到了，接着继续"), "继续")
    ck("【求助】", G.classify("【求助】这个路径我确定不了"), "求助")

    print()
    print("=== 二、分类（无标记，靠词兜底）")
    ck("兜底-求助", G.classify("你帮我查一下 18097 到底绑哪个 IP"), "求助")
    ck("兜底-意见", G.classify("这一处的数字要改成 14:16"), "意见")
    ck("兜底-继续", G.classify("我找到了，接着继续吧"), "继续")
    ck("兜底-纯闲聊→牢骚", G.classify("今天好累啊，随便说一句"), "牢骚")
    # 2026-11-11 22:2x 实测补：用户只说一个字的推进令（当时"动"被判成牢骚，判反了）
    ck("极短推进令-动", G.classify("动"), "继续")
    ck("极短推进令-继续", G.classify("继续"), "继续")
    ck("极短推进令-做吧", G.classify("做吧"), "继续")
    ck("嗯 不算推进令", G.classify("嗯"), "牢骚")
    # 2026-11-11 23:1x 补：内部守则（略） §十 要求她每条带「【飞书助手】」前缀，分类前必须先剥掉它
    def _strip(body: str) -> str:
        b = body.lstrip()
        for p in ("【飞书助手】", "【用户】"):
            if b.startswith(p):
                b = b[len(p):].lstrip()
        return b


    ck("前缀+短令-飞书助手动", G.classify(_strip("【飞书助手】动")), "继续")
    ck("前缀+意见", G.classify(_strip("【飞书助手】【意见】L135 改掉")), "意见")
    ck("前缀+求助", G.classify(_strip("【飞书助手】【求助】帮我查一下路径")), "求助")
    ck("前缀+用户动", G.classify(_strip("【用户】动")), "继续")

    print()
    print("=== 三、判定（正向）")
    ck("空队列 → IDLE",
       G.decide([], None, NOW)[0], "IDLE")
    ck("1 条求助 → PROCEED",
       G.decide([mk("求助", 120)], None, NOW)[0], "PROCEED")
    ck("2 意见 + 1 牢骚（已 settled）→ BATCH",
       G.decide([mk("意见", 180), mk("意见", 150), mk("牢骚", 120)], None, NOW)[0], "BATCH")
    ck("3 条全牢骚（已 settled）→ SILENT",
       G.decide([mk("牢骚", 200), mk("牢骚", 150), mk("牢骚", 120)], None, NOW)[0], "SILENT")
    ck("[牢骚, 继续] → PROCEED",
       G.decide([mk("牢骚", 180), mk("继续", 120)], None, NOW)[0], "PROCEED")

    print()
    print("=== 四、反向用例（错的必须被判成错的）")

    # 反向 1：WAIT 必须可达 —— 这是第一版留下的死代码，就是这条抓出来的
    ck("★WAIT 可达（刚收到的 2 条）",
       G.decide([mk("牢骚", 20), mk("牢骚", 5)], None, NOW)[0], "WAIT")

    # 反向 2：单条牢骚不许触发 BATCH（上午最大的浪费）
    ck("1 条牢骚 不 BATCH",
       G.decide([mk("牢骚", 120)], None, NOW)[0] != "BATCH", True)

    # 反向 3：有求助时，一堆意见也不许攒着批处理
    ck("求助优先于 BATCH",
       G.decide([mk("意见", 200), mk("意见", 180), mk("求助", 120)], None, NOW)[0], "PROCEED")

    # 反向 4：还在补（最新 5 秒前）时，不许急着 BATCH
    ck("刚收到 2 意见 → WAIT 不 BATCH",
       G.decide([mk("意见", 40), mk("意见", 5)], None, NOW)[0], "WAIT")

    # 反向 5：settled 之后的意见必须 BATCH，不能被 WAIT 劫走
    ck("settled 的 2 意见 → BATCH 不 WAIT",
       G.decide([mk("意见", 300), mk("意见", 240)], None, NOW)[0], "BATCH")

    # 反向 6：空队列绝不能被判成 PROCEED（否则会空转）
    ck("空队列 不是 PROCEED",
       G.decide([], None, NOW)[0] != "PROCEED", True)

    # 反向 7：settled 的全牢骚不许判成 PROCEED（会被推着去干活）
    ck("全牢骚 settled 不是 PROCEED",
       G.decide([mk("牢骚", 300), mk("牢骚", 240)], None, NOW)[0] != "PROCEED", True)

    # 反向 8：有未处理意见时，"继续"不能把意见吞掉
    ck("意见未处理时 继续 不吞意见",
       G.decide([mk("意见", 300), mk("继续", 120)], None, NOW)[0], "BATCH")

    print()
    if fails:
        print("FAILED %d 项：%s" % (len(fails), fails))
        raise SystemExit(1)
    print("全过：%d 项断言" % count[0])