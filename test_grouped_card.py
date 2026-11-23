import _testenv; _testenv.sandbox()

# -*- coding: utf-8 -*-
"""分组卡断言 —— 夹具驱动（2026-11-22 桌面助手改造）。

原为活体数据脚本（断言特定 Porta 会话标题在场且最新），会话一归档即塌
（GPT UNC 补丁轮回归发现：示例协作窗口/Video/讨论等窗当晚已 archived）。
改为合成夹具确定性覆盖原全部断言语义；活体只做结构冒烟＋动态保护/归档排除
不变量（不依赖任何特定标题）。原活体版备份 .bak-20261122-fixture。
"""
import sys, copy
sys.path.insert(0, r'项目根\tools')
import picker, window_picker as wp

if __name__ == "__main__":


    def C(id, title, **kw):
        base = dict(leg="porta", hostId="antigravity", id=id, title=title,
                    alias="", display_title=title, role="peer", cwd="",
                    status="idle", lastActivity="2026-11-22T22:00:00Z",
                    busySecondsAgo=3600, reachable=True, unusableReason="",
                    truncated=False, meta={})
        base.update(kw)
        return wp.UnifiedCandidate(**base)


    # 夹具模拟活体形状：协作窗最新居首、演示窗、讨论窗、受保护工作区窗、归档窗
    cs = [
        C("00000000-0000-0000-0000-000000000000", "示例协作窗口",
          lastActivity="2026-11-22T22:50:00Z"),
        C("11111111-0000-0000-0000-000000000001", "Demo Video Planning",
          lastActivity="2026-11-22T21:00:00Z"),
        C("22222222-0000-0000-0000-000000000002", "架构方案讨论",
          lastActivity="2026-11-22T20:00:00Z"),
        C("33333333-0000-0000-0000-000000000003", "受保护工作区示例",
          unusableReason="workspace_protected", reachable=False,
          cwd="X:/work/protected-workspace"),
        C("44444444-0000-0000-0000-000000000004", "Archived Session Demo",
          status="archived", unusableReason="archived", reachable=False),
    ]
    card = wp.build_card_grouped(cs)
    qs = card["questions"]
    headers = [q["header"] for q in qs]
    print("grouped=", card.get("grouped"), "total=", card["total"], "headers=", headers)
    assert card.get("grouped") is True
    assert card["total"] == 3, "保护＋归档预排除"
    assert "会话区" in headers
    all_opt = [o["label"] for q in qs for o in q["options"]]
    assert "会话区" not in all_opt, "group name must be header NOT option"
    assert not any(l.startswith("〔项目〕") for l in all_opt)
    assert "示例协作窗口" in all_opt
    assert not any("受保护" in l for l in all_opt), "protected excluded"
    assert not any("Archived Session" in l for l in all_opt), "archived excluded"
    sess_q = [q for q in qs if q["id"] == "grp_session"][0]
    assert sess_q["options"][0]["label"] == "示例协作窗口", "输入序（活体由适配器按最近活跃排）"
    code, cand = wp.resolve_grouped(card, [{"id": "grp_session", "selected": ["示例协作窗口"]}, {"id": "nav", "selected": []}])
    assert code == "ok" and cand.title == "示例协作窗口"
    assert wp.resolve_grouped(card, [{"id": "nav", "selected": [wp.NAV_CANCEL]}])[0] == "cancelled"
    assert wp.resolve_grouped(card, [{"id": "nav", "selected": [wp.NAV_KEYWORD]}])[0] == "keyword"
    assert wp.resolve_grouped(card, [{"id": "grp_session", "selected": []}])[0] == "cancelled", "全跳过＝不投"
    assert wp.resolve_grouped(card, [{"id": "grp_session", "selected": [], "custom": "取消"}])[0] == "cancelled", "打取消＝不投"
    # 第一题直接打字＝关键字筛选（用户 2026-11-22）
    assert wp.resolve_grouped(card, [{"id": "grp_session", "selected": [], "custom": "视频"}]) == ("keyword", "视频")
    assert wp.resolve_grouped(card, [{"id": "grp_session", "selected": ["示例协作窗口"], "custom": "Video"}]) == ("keyword", "Video"), "custom 优先"
    fc = wp.build_card_grouped_filtered(cs, "Video")
    fc_labels = [o["label"] for q in fc["questions"] for o in q["options"]]
    assert "Demo Video Planning" in fc_labels, "keyword Video hits"
    assert "示例协作窗口" not in fc_labels, "non-match excluded"
    fc2 = wp.build_card_grouped_filtered(cs, "协作")
    fc2_labels = [o["label"] for q in fc2["questions"] for o in q["options"]]
    assert "示例协作窗口" in fc2_labels, "keyword 协作 hits"
    assert not any(q["id"] == "nav" for q in qs), "操作题已去掉（用户 2026-11-22）"
    cs2 = copy.deepcopy(cs)
    for c in cs2:
        if c.title == "架构方案讨论":
            c.meta["projectName"] = "演示项目"
    card2 = wp.build_card_grouped(cs2)
    h2 = [q["header"] for q in card2["questions"]]
    assert "项目 · 演示项目" in h2, "project group header"
    assert h2.index("项目 · 演示项目") < h2.index("会话区"), "项目 before 会话"
    proj_q = [q for q in card2["questions"] if q["header"] == "项目 · 演示项目"][0]
    assert "架构方案讨论" in [o["label"] for o in proj_q["options"]]
    card3 = wp.build_card_grouped(cs, scroll_cap=2)
    assert card3.get("grouped") is False, "over cap fallback"
    assert len(card3["questions"]) == 1
    # ── 活体结构冒烟：不依赖特定标题，只核 schema 与动态排除不变量 ──
    ca = picker.card_grouped()
    assert isinstance(ca.get("total"), int) and "questions" in ca
    assert all(q.get("id") and q.get("question") and isinstance(q.get("options"), list) for q in ca["questions"])
    if ca["total"] <= 60:
        assert ca.get("grouped") is True
    live = picker.gather()
    # 按候选键逐行核（不用标题集合：同名窗一条归档一条可投会假阳性）
    for r in ca.get("rows", []):
        ok, reason = wp.verify_target(r["candidate"], allow_protected=False)
        assert ok, "活体卡行必须合格: %s (%s)" % (reason, r["candidate"].key)
    # 路 B 分页兜底无 rows 时，按标签解析回候选再核
    if not ca.get("rows"):
        for q in ca["questions"]:
            for o in q["options"]:
                if o["label"] in (wp.NAV_NEXT, wp.NAV_KEYWORD, wp.NAV_CANCEL):
                    continue
                act, cnd = wp.resolve_selection(ca, o["label"])
                assert act == "ok" and cnd is not None
                ok, reason = wp.verify_target(cnd, allow_protected=False)
                assert ok, "活体卡行必须合格: %s" % reason
    print("GROUPED MQ CARD ASSERTIONS ALL PASS (fixture + live structural smoke)")
