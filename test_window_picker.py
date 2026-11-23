
import os
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import window_picker as wp

if __name__ == "__main__":

    def C(**kw):
        base = dict(leg="codex", hostId="h1", id="id-" + kw.get("title", "x"), reachable=True)
        base.update(kw)
        return wp.UnifiedCandidate(**base)

    n = 0
    def ok(cond, label):
        global n
        assert cond, "FAIL: " + label
        n += 1
        print("  OK ", label)

    print("=== verify_target ===")
    ok(wp.verify_target(C())[0], "reachable passes")
    ok(not wp.verify_target(C(reachable=False, unusableReason="archived"))[0], "archived rejected")
    ok(not wp.verify_target(C(reachable=False, unusableReason="cloud_no_rollout"))[0], "cloud rejected")
    ok(not wp.verify_target(C(reachable=False, unusableReason="workspace_protected"))[0], "protected rejected")
    ok(wp.verify_target(C(reachable=False, unusableReason="workspace_protected"), allow_protected=True)[0], "protected allowed with flag")

    print("=== keyword_match ===")
    cands = [C(title="示例协作测试窗口", id="a"), C(title="健康计划", id="b"),
             C(title="示例协作测试窗口", id="a"),
             C(leg="porta", hostId="local", id="c", title="受保护批次", cwd="file:///X:/%E4%BF%9D%E6%8A%A4%E5%8C%BA/x", meta={"projectName": "保护区"})]
    hits = wp.keyword_match(cands, "协作")
    ok(len(hits) == 1 and hits[0].id == "a", "substring unique + dedup")
    ok(len(wp.keyword_match(cands, "保护区")) == 1, "URL-decoded cwd / projectName match")
    ok(wp.keyword_match(cands, "不存在词") == [], "zero hit")
    ok(len(wp.keyword_match(cands, "测试")) == 1, "no fuzzy: 测试 exact substring match")

    print("=== select orchestrator ===")
    code, payload = wp.select([C(title="唯一", id="u")])
    ok(code == wp.EXIT_OK and payload.id == "u", "single qualified -> OK")
    code, payload = wp.select([C(title="甲", id="1"), C(title="乙", id="2")])
    ok(code == wp.EXIT_AMBIGUOUS and len(payload) == 2, "multiple -> ambiguous (no guess)")
    code, payload = wp.select([C(title="甲", id="1"), C(title="乙", id="2")], keyword="甲")
    ok(code == wp.EXIT_OK and payload.id == "1", "keyword narrows to unique")
    code, payload = wp.select([C(title="归档", id="g", reachable=False, unusableReason="archived")], explicit="g")
    ok(code == wp.EXIT_UNQUALIFIED, "explicit ID still verified (bug2 fix)")
    code, payload = wp.select([])
    ok(code == wp.EXIT_NO_TARGET, "zero -> no_target")
    code, payload = wp.select([C(title="重名", id="r1"), C(title="重名", id="r2")], explicit="重名")
    ok(code == wp.EXIT_AMBIGUOUS, "duplicate alias -> ambiguous (bug1 fix)")

    print("=== prefilter_active (UX §12) ===")
    active = wp.prefilter_active([C(title="活", id="live"), C(title="归档", id="arch", reachable=False, unusableReason="archived")])
    ok(len(active) == 1 and active[0].id == "live", "archived pre-excluded before card")

    print("=== truncated forbids auto-unique ===")
    code, payload = wp.select([C(title="唯一但截断", id="t", truncated=True)])
    ok(code == wp.EXIT_AMBIGUOUS, "truncated source -> no auto-unique")


    print("=== selection card (User UX) ===")
    many = [C(title="对话%d" % i, id="id%d" % i, status="idle", cwd="F:/proj/x") for i in range(20)]
    card = wp.build_card(many, page=0)
    ok(card["total"] == 20 and card["pages"] == 3, "20 active -> 3 pages of 8")
    ok(len(card["question"]["options"]) == 8 + 3, "page0: 8 rows + next/keyword/cancel")
    ok(card["question"]["options"][-1]["label"] == wp.NAV_CANCEL, "cancel always present")
    kind, _ = wp.resolve_selection(card, wp.NAV_CANCEL)
    ok(kind == "cancelled", "cancel -> cancelled (exit10 upstream)")
    kind, _ = wp.resolve_selection(card, wp.NAV_NEXT)
    ok(kind == "next_page", "next page token")
    kind, cand = wp.resolve_selection(card, "对话0")
    ok(kind == "ok" and cand.id == "id0", "label maps back to candidate")
    card2 = wp.build_card(many, page=2)
    ok(len([o for o in card2["question"]["options"] if o["label"].startswith("对话")]) == 4, "last page 4 rows")
    dup = [C(title="重名窗", id="aaaa1111", status="idle"), C(title="重名窗", id="bbbb2222", status="running")]
    dcard = wp.build_card(dup)
    labels = [r["label"] for r in dcard["rows"]]
    ok(len(set(labels)) == 2 and all("(" in l for l in labels), "duplicate titles get unique short-id labels")
    ok(any("aaaa" in r["description"] for r in dcard["rows"]), "duplicate subtitle carries short id")
    arch = wp.build_card([C(title="活", id="live"), C(title="归档", id="arch", reachable=False, unusableReason="archived")])
    ok(arch["total"] == 1, "archived pre-excluded from card")

    print("\n全过：%d 项断言" % n)
