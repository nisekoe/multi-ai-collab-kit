import _testenv; _testenv.sandbox()

import os
import sys
# -*- coding: utf-8 -*-
"""dispatch_for_slot 接线验收 v2（GPT v0.3 复审 #1/#2/#4/#5/#6 修复后，2026-11-23）。
真判达才产证据且归属本次提交（nonce/cid）；porta POST 前最终复验全键/角色/资格/cwd；
忙态与权威拒绝直接返回不消费回执；pin 贯穿不回落；v2 卡走 v2 解码器。"""
import json, os, sys, tempfile, time, types
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import picker, reach_gpt, porta_adapter as pa, window_picker as wp

if __name__ == "__main__":
    if os.environ.get("MACK_HOST_TESTS") != "1":
        print("skip：依赖宿主机状态，设 MACK_HOST_TESTS=1 再跑")
        sys.exit(0)

    FAILS = []
    def ok(cond, msg):
        print(("  OK  " if cond else "  FAIL ") + msg)
        if not cond:
            FAILS.append(msg)

    def C(leg, id="t-1", host="local", cwd="", role="peer"):
        return wp.UnifiedCandidate(leg=leg, hostId=host, id=id, title="T", cwd=cwd,
                                   status="idle", reachable=True, role=role)

    def er_with(cands, ok_flag=True, host="antigravity"):
        return types.SimpleNamespace(ok=ok_flag, candidates=cands, error="", truncated=False, hostId=host)

    tmp = tempfile.mkdtemp()
    rp = os.path.join(tmp, "receipt.json")
    def write_receipt(rows):
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    print("=== porta：判达＋cid 归属 ===")
    cp = C("porta", "conv1", "antigravity")
    fresh_ok = C("porta", "conv1", "antigravity")
    rec_v = {"ok": True, "stage": "verified", "reason": "delivered_confirmed",
             "matchedBodyFingerprint": True, "matchedClientMessageId": True,
             "clientMessageId": "porta-m-test-001", "at": "2026-11-23T00:10:00+08:00",
             "hostId": "antigravity", "convId": "conv1"}
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ev = p.get("delivery_evidence") or {}
    ok(code == 0 and ev.get("evidence_type") == "porta_verified_step" and ev.get("message_id") == "porta-m-test-001"
       and ev.get("target_key") == ["porta", "antigravity", "conv1"] and ev.get("verified") is True,
       "porta verified+cid → evidence")
    rec_run = dict(rec_v, stage="delivered_running")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, rec_run)):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == 0 and (p.get("delivery_evidence") or {}).get("evidence_type") == "porta_verified_step",
       "porta delivered_running+cid → evidence")
    rec_nocid = dict(rec_v, matchedClientMessageId=False)
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, rec_nocid)):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and "delivery_evidence" not in p and p.get("reason") == "porta_step_association_missing"
       and p.get("landed") is True and "勿重投" in p.get("note", ""),
       "porta fp命中cid未对上 → 不产证据＋勿重投（GPT#1）")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, {"ok": True, "stage": "dry_run", "dry_run": True})) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=True)
    ok("delivery_evidence" not in p and md.call_args.kwargs.get("dry_run") is True, "porta dry_run → no evidence")

    print("=== porta：POST 前最终复验（GPT#2） ===")
    fresh_otherhost = C("porta", "conv1", "fixture-other-host")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_otherhost])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "host_mismatch" and md.call_count == 0
       and "delivery_evidence" not in p, "porta host漂移 → 6 host_mismatch 零POST")
    fresh_delegated = C("porta", "conv1", "antigravity", role="delegated")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_delegated])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "role_mismatch" and md.call_count == 0,
       "porta role漂移delegated → 6 role_mismatch 零POST")
    fresh_unknown = C("porta", "conv1", "antigravity", role="unknown")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_unknown])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "identity_missing" and md.call_count == 0,
       "porta fresh unknown → 2 identity_missing（缺身份待补证据，GPT桥#5）")
    cp_cwd = C("porta", "conv1", "antigravity", cwd="fixture/selected")
    fresh_cwd = C("porta", "conv1", "antigravity", cwd="fixture/other")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_cwd])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp_cwd, text="hi", dry_run=False)
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "cwd_drift" and md.call_count == 0,
       "porta cwd漂移 → 6 cwd_drift 零POST")
    fresh_prot = C("porta", "conv1", "antigravity")
    fresh_prot.unusableReason = "workspace_protected"
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_prot])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "workspace_protected" and md.call_count == 0,
       "porta 保护窗 → 6 workspace_protected 零POST")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "no_local_record" and md.call_count == 0,
       "porta 目标消失 → 6 no_local_record 零POST")
    rec_wronghost = dict(rec_v, hostId="fixture-other-host")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, rec_wronghost)):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "post_receipt_mismatch" and p.get("landed") is True
       and "delivery_evidence" not in p and "勿重投" in p.get("note", ""),
       "porta 回执host与选中不符 → 2待核勿重投（POST已发生，GPT桥#2）")

    print("=== codex：nonce 归属本次提交（GPT#1） ===")
    cc = C("codex", "00000000-0000-0000-0000-000000000001", "local", "F:/w")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with mock.patch.object(picker.archive_paths, "resolve", return_value=rp):
        # exit7 忙态＋同秒旧回执（无本次nonce）→ 绝不归一为已送达
        write_receipt([{"at": now, "thread": cc.id, "mode": "queue", "正文进了": True, "nonce": "OLD-NONCE"}])
        with mock.patch.object(picker, "commit_codex", return_value=(7, {"ok": False, "exit": 7, "nonce": "NEW-NONCE", "output": "看起来他正在用那个窗口", "reason": "busy_window"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
        ok(code == 7 and p.get("reason") == "busy_window" and "delivery_evidence" not in p and p.get("ok") is False,
           "codex exit7＋同秒旧回执 → 7 busy 零证据（GPT探针场景）")
        # exit5＋本次nonce回执landed → 归一ok＋勿重投
        write_receipt([{"at": now, "thread": cc.id, "正文进了": True, "nonce": "N5"}])
        with mock.patch.object(picker, "commit_codex", return_value=(5, {"ok": False, "exit": 5, "nonce": "N5", "output": "正文进没进：进了｜有没有新回复：没读到", "reason": "delivered_running"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False, expected_cwd="F:/w", registry_path="F:/reg.json")
        ev = p.get("delivery_evidence") or {}
        ok(code == 0 and p.get("ok") is True and ev.get("message_id") == "gpt-receipt:N5"
           and ev.get("evidence_type") == "codex_reach_gpt_verified" and "勿重投" in p.get("note", ""),
           "codex exit5＋本次nonce landed → ok＋evidence＋勿重投")
        # 错误nonce回执（同thread同秒）→ 不归属
        write_receipt([{"at": now, "thread": cc.id, "正文进了": True, "nonce": "OTHER"}])
        with mock.patch.object(picker, "commit_codex", return_value=(5, {"ok": False, "exit": 5, "nonce": "MINE", "output": "正文进没进：没查到"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
        ok(code == 5 and "delivery_evidence" not in p and p.get("ok") is False,
           "codex 同秒异nonce回执 → 不归属本次（GPT#1）")
        # exit0＋本次nonce → evidence
        write_receipt([{"at": now, "thread": cc.id, "正文进了": True, "nonce": "N0"}])
        with mock.patch.object(picker, "commit_codex", return_value=(0, {"ok": True, "exit": 0, "nonce": "N0", "output": "正文进没进：进了｜有"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
        ok(code == 0 and (p.get("delivery_evidence") or {}).get("verified") is True, "codex exit0＋nonce → evidence")
        # 权威拒绝 exit6 结构化 reason 原样上抛
        with mock.patch.object(picker, "commit_codex", return_value=(6, {"ok": False, "reason": "workspace_protected", "nonce": "N6", "stage": "commit_reverify"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
        ok(code == 6 and p.get("reason") == "workspace_protected" and "delivery_evidence" not in p
           and p.get("target_key") == ["codex", "local", cc.id], "codex exit6 结构化reason＋target_key上抛")
        # 超时晚到＋nonce回执landed → 认送达
        write_receipt([{"at": now, "thread": cc.id, "正文进了": True, "nonce": "NT"}])
        with mock.patch.object(picker, "commit_codex", return_value=(wp.EXIT_ENV, {"ok": False, "reason": "commit_timeout", "timeout": True, "nonce": "NT", "exit": None, "output": ""})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
        ok(code == 0 and p.get("ok") is True and (p.get("delivery_evidence") or {}).get("message_id") == "gpt-receipt:NT",
           "codex 超时晚到＋nonce landed → 认送达（GPT#5）")
        # 超时晚到无回执 → 瞬时 need_evidence
        write_receipt([])
        with mock.patch.object(picker, "commit_codex", return_value=(wp.EXIT_ENV, {"ok": False, "reason": "commit_timeout", "timeout": True, "nonce": "NT2", "exit": None, "output": ""})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
        ok(code == wp.EXIT_ENV and p.get("reason") == "commit_timeout" and "delivery_evidence" not in p,
           "codex 超时无回执 → commit_timeout 瞬时")
        # 机器确认landed但无本次nonce回执 → 待核勿重投不产假证据（GPT第三轮#1）
        with mock.patch.object(picker, "commit_codex", return_value=(0, {"ok": True, "exit": 0, "nonce": "NX", "landed_hint": True, "output": "机器确认落盘"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
        ok(code == wp.EXIT_ENV and p.get("reason") == "codex_receipt_missing" and p.get("landed") is True
           and "delivery_evidence" not in p and "勿重投" in p.get("note", ""), "codex 机器landed缺回执 → 待核勿重投不产假证据（GPT第三轮#1）")
        # dry_run → commit dry_run 无证据
        with mock.patch.object(picker, "commit_codex", return_value=(0, {"ok": True, "dry_run": True, "nonce": "ND", "output": "dry-run"})) as mc:
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=True)
        ok(mc.call_args.kwargs.get("dry_run") is True and "delivery_evidence" not in p, "codex dry_run → no evidence")

    print("=== commit_codex：nonce/registry 贯穿桥子进程（GPT#4） ===")
    key = ("codex", cc.hostId, cc.id)
    snap = types.SimpleNamespace(threads={key: {"cwd": "F:/w"}})
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        if "--result-file" in cmd:
            with open(cmd[cmd.index("--result-file") + 1], "w", encoding="utf-8") as f:
                json.dump({"exit": 6, "reason": "cwd_drift", "nonce": cmd[cmd.index("--nonce") + 1], "thread": cc.id}, f)
        return types.SimpleNamespace(returncode=6, stdout="投前工作区已变更\nREASON=cwd_drift", stderr="")
    with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap), \
         mock.patch("codex_picker_adapter.verify_target", return_value=types.SimpleNamespace(reason=None)), \
         mock.patch("subprocess.run", side_effect=fake_run):
        code, p = picker.commit_codex(cc, "hi", dry_run=False, expected_cwd="F:/w", registry_path="F:/pinned/reg.json")
    cmd = captured.get("cmd") or []
    ok("--nonce" in cmd and "--registry" in cmd and "--result-file" in cmd
       and Path(cmd[cmd.index("--registry") + 1]) == Path("F:/pinned/reg.json")
       and p.get("nonce") and code == 6 and p.get("reason") == "cwd_drift",
       "commit_codex 传 --nonce/--registry/--result-file＋机器结果结构化（GPT桥#5）")

    print("=== reach_gpt：完整正文判达（GPT#1） ===")
    rollout = os.path.join(tmp, "rollout.jsonl")
    shared = "shared first forty characters are identical across distinct bodies"
    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with open(rollout, "w", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": ts_iso, "type": "response_item",
                            "payload": {"type": "message", "role": "user",
                                        "content": [{"text": reach_gpt.with_prefix(shared + " first")}]}}) + "\n")
    ok(reach_gpt.saw_my_message(rollout, reach_gpt.with_prefix(shared + " second"), time.time()) is False,
       "同40字前缀异文 → 不判达（GPT探针场景）")
    ok(reach_gpt.saw_my_message(rollout, reach_gpt.with_prefix(shared + " first"), time.time()) is True,
       "完整正文相同 → 判达")

    print("=== porta：全长核心指纹（GPT#1） ===")
    h = "公共接棒头：项目进展同步，以下为本次正文。"
    fp_a = pa.extract_body_fingerprint(pa.wrap_origin_idempotent(h + " AAA 内容甲", "desktop_bot"))
    fp_b = pa.extract_body_fingerprint(pa.wrap_origin_idempotent(h + " BBB 内容乙", "desktop_bot"))
    fp_a2 = pa.extract_body_fingerprint(pa.wrap_origin_idempotent(h + " AAA 内容甲", "desktop_bot"))
    ok(fp_a != fp_b and fp_a == fp_a2, "公共头异文指纹不同／同文指纹相同")

    print("=== gather_with_status：pin 不回落（GPT#4） ===")
    with mock.patch.object(picker, "enumerate_codex", side_effect=TypeError("boom")) as mec, \
         mock.patch.object(picker, "enumerate_porta", return_value=([], False, "", "")), \
         mock.patch.object(picker, "enumerate_harbor", return_value=([], False, "harbor_deferred")):
        g = picker.gather_with_status(registry_path="F:/pinned.json")
    ok(mec.call_count == 1 and mec.call_args.kwargs.get("registry_path") == "F:/pinned.json"
       and g["legs"]["codex"]["ok"] is False and "TypeError" in g["legs"]["codex"]["error"],
       "pin枚举内部TypeError → 不回落默认册，腿标失败")

    print("=== 生产解析器：v2 卡守卫（GPT#6） ===")
    act, cand = wp.resolve_grouped({"option_map": {}, "rows": [], "questions": []}, [{"id": "q", "selected": ["X"]}])
    ok(act == "v2_card_use_v2_resolver" and cand is None, "v2卡进旧解析器 → 拒解走v2")
    act2, _ = wp.resolve_grouped({"rows": [], "questions": []}, [{"id": "q", "selected": [wp.NAV_NEXT]}])
    ok(act2 == "next_page", "v1卡翻页分支不受影响")

    print("=== harbor stub / unknown leg ===")
    code, p = picker.dispatch_for_slot(candidate=C("harbor", "s1", "local"), text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "harbor_delivery_stub" and p.get("target_key")
       and "delivery_evidence" not in p, "harbor stub 拒投＋target_key")
    code, p = picker.dispatch_for_slot(candidate=C("mars"), text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "unknown_leg", "unknown leg 拒")

    print("=== 证据过实验区 validate_delivery_evidence（跨区契约） ===")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import project_slots as ps
        with mock.patch.object(picker.archive_paths, "resolve", return_value=rp):
            write_receipt([{"at": now, "thread": cc.id, "正文进了": True, "nonce": "NV"}])
            with mock.patch.object(picker, "commit_codex", return_value=(0, {"ok": True, "exit": 0, "nonce": "NV", "output": "进了"})):
                code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
            evd = ps.validate_delivery_evidence(p["delivery_evidence"], expected_target_key=("codex", "local", cc.id))
            ok(evd.get("evidence_type") == "codex_reach_gpt_verified", "codex evidence 过评审助手 validate")
        with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
             mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v))):
            code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
        evd2 = ps.validate_delivery_evidence(p["delivery_evidence"], expected_target_key=("porta", "antigravity", "conv1"))
        ok(evd2.get("evidence_type") == "porta_verified_step", "porta evidence 过评审助手 validate")
    except ImportError as exc:
        print("  SKIP 实验区契约校验:", exc)

    print("=== 第三轮桥复审新增反例（GPT 2026-11-23） ===")
    # porta 截断缺席 → 2 待核（非权威6）
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=types.SimpleNamespace(ok=True, candidates=[], error="", truncated=True, hostId="antigravity")), \
         mock.patch.object(pa.PortaAdapter, "deliver", side_effect=AssertionError("must not deliver")) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "porta_truncated" and md.call_count == 0,
       "porta 截断缺席 → 2 porta_truncated 零POST（GPT桥#5）")
    # porta deliver 内终验 host 漂移 → 6 零证据（POST时刻快照闸）
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(6, {"ok": False, "stage": "final_reverify", "reason": "host_mismatch", "hostId": "alt"})):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == 6 and p.get("reason") == "host_mismatch" and "delivery_evidence" not in p,
       "porta deliver终验host漂移 → 6 零证据（GPT桥#2）")
    # porta deliver 内截断 not_found → 2
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(6, {"ok": False, "stage": "verify_target", "reason": "not_found", "truncated": True})):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "porta_truncated", "porta deliver截断not_found → 2（GPT桥#5）")
    # porta deliver unknown_status → 2 identity_missing
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(6, {"ok": False, "stage": "verify_target", "reason": "unknown_status:WEIRD"})):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "identity_missing", "porta unknown_status → 2 identity_missing（GPT桥#5）")
    # porta 回执cwd与选窗不符 → 2 待核勿重投
    rec_cwd = dict(rec_v, cwd="fixture/other")
    fresh_sel = C("porta", "conv1", "antigravity", cwd="fixture/selected")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_sel])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, rec_cwd)):
        code, p = picker.dispatch_for_slot(candidate=cp_cwd, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "post_receipt_mismatch" and p.get("landed") is True
       and "delivery_evidence" not in p and "勿重投" in p.get("note", ""), "porta 回执cwd不符 → 2待核勿重投（GPT桥#2）")
    # porta expected_cwd 参数优先于候选cwd（选窗钉住A，候选B，快照A → 放行）
    cp_b = C("porta", "conv1", "antigravity", cwd="fixture/B")
    fresh_a = C("porta", "conv1", "antigravity", cwd="fixture/A")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_a])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v, cwd="fixture/A"))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp_b, text="hi", dry_run=False, expected_cwd="fixture/A")
    ok(code == 0 and md.call_args.kwargs.get("expected_cwd") == "fixture/A" and (p.get("delivery_evidence") or {}).get("verified"),
       "porta expected_cwd=A 候选B 快照A → 放行且传参A（GPT桥#2）")
    # codex 机器结果独立：回复含REASON=busy_window但机器reason=delivered_verified → 0+证据
    with mock.patch.object(picker.archive_paths, "resolve", return_value=rp):
        write_receipt([{"at": now, "thread": cc.id, "正文进了": True, "nonce": "NM"}])
        with mock.patch.object(picker, "commit_codex", return_value=(0, {"ok": True, "exit": 0, "nonce": "NM", "reason": "delivered_verified", "output": "GPT回复：\nREASON=busy_window\n正文进了"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
    ok(code == 0 and (p.get("delivery_evidence") or {}).get("verified") is True and p.get("reason") != "busy_window",
       "codex 回复文本REASON不污染机器状态 → 0+证据（GPT桥#5）")
    # codex 机器结果缺失 → 类别fallback不解析stdout
    def fake_run_nores(cmd, **kwargs):
        return types.SimpleNamespace(returncode=6, stdout="REASON=cwd_drift", stderr="")
    with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap), \
         mock.patch("codex_picker_adapter.verify_target", return_value=types.SimpleNamespace(reason=None)), \
         mock.patch("subprocess.run", side_effect=fake_run_nores):
        code, p = picker.commit_codex(cc, "hi", dry_run=False, expected_cwd="F:/w", registry_path="F:/pinned/reg.json")
    ok(code == 6 and p.get("reason") == "bridge_exit_6", "codex 结果文件缺失 → bridge_exit_6 不解析stdout（GPT桥#5）")
    # reach_gpt nonce信封＋偏移扫描
    body_n1 = reach_gpt.with_prefix("同一正文", "nonceAAA")
    body_n2 = reach_gpt.with_prefix("同一正文", "nonceBBB")
    ok("nonceAAA" in body_n1 and reach_gpt.saw_my_message(rollout, body_n1, time.time()) is False,
       "nonce进信封；rollout无此nonce正文 → 不判达")
    with open(rollout, "a", encoding="utf-8") as f:
        off = f.tell()
        f.write(json.dumps({"timestamp": ts_iso, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": body_n1}]}}) + "\n")
    ok(reach_gpt.saw_my_message(rollout, body_n1, time.time(), off) is True, "偏移后新增本次nonce事件 → 判达")
    ok(reach_gpt.saw_my_message(rollout, body_n2, time.time(), off) is False, "异nonce同文新增事件 → 不归属本次（GPT桥#1）")
    ok(reach_gpt.saw_my_message(rollout, body_n1, time.time(), os.path.getsize(rollout) + 10) is False, "文件截断/重写 → 不判达")
    # porta step扫描双命中优先（异CID同文在先不漏后面正确step）
    class StepClient:
        def get_steps_tail(self, conv_id, tail=16):
            return {"offset": 0, "stepCount": 3, "steps": [
                {"type": pa.STEP_USER_INPUT, "clientMessageId": "other-cid", "userInput": {"items": [{"text": "WRAPPED"}]}},
                {"type": pa.STEP_USER_INPUT, "clientMessageId": "my-cid", "userInput": {"items": [{"text": "WRAPPED"}]}},
                {"type": pa.STEP_PLANNER_RESPONSE, "status": pa.STEP_STATUS_DONE, "plannerResponse": {"response": "r"}},
            ]}
    adapter_s = pa.PortaAdapter(client=StepClient())
    fp_s = pa.extract_body_fingerprint("WRAPPED")
    insp = adapter_s._inspect_delivery_steps("conv1", 0, fp_s, "my-cid")
    ok(insp.get("matchedBodyFingerprint") and insp.get("matchedClientMessageId") and insp.get("userStepIndex") == 1,
       "porta 异CID同文在先 → 扫全批取双命中idx1（GPT桥#1）")
    # gather 覆盖同源枚举（空结果host A不得借健康检查标B）
    with mock.patch.object(picker, "enumerate_codex", return_value=([], False, "")), \
         mock.patch.object(picker, "enumerate_porta", return_value=([], False, "", "host-A")), \
         mock.patch.object(picker, "enumerate_harbor", return_value=([], False, "deferred")):
        g = picker.gather_with_status()
    ok(g["legs"]["porta"]["hosts"] == ["host-A"] and g["legs"]["porta"]["ok"], "porta 空结果覆盖=同次枚举hostA（GPT桥#5）")
    # v2 守卫按字段存在（option_map=None畸形卡也拒）
    act3, _ = wp.resolve_grouped({"option_map": None, "rows": [], "questions": []}, [{"id": "q", "selected": ["X"]}])
    ok(act3 == "v2_card_use_v2_resolver", "option_map=None畸形卡 → 仍拒解走v2（GPT桥#6）")

    print("=== 第三轮生产桥复审新增反例（GPT 2026-11-23 v3） ===")
    # codex 机器landed=False＋stdout称进了 → 不翻landed（GPT第三轮#1）
    with mock.patch.object(picker.archive_paths, "resolve", return_value=rp):
        write_receipt([{"at": now, "thread": cc.id, "正文进了": False, "nonce": "NF"}])
        with mock.patch.object(picker, "commit_codex", return_value=(5, {"ok": False, "exit": 5, "nonce": "NF", "landed_hint": False, "output": "正文进没进：进了"})):
            code, p = picker.dispatch_for_slot(candidate=cc, text="hi", dry_run=False)
    ok(code == 5 and p.get("landed") is False and "delivery_evidence" not in p and p.get("reason") != "codex_receipt_missing",
       "codex 机器landed=False＋stdout称进了 → landed不翻（GPT第三轮#1）")
    # codex 机器结果nonce不归属 → 不采信（commit级）
    def fake_run_wrongnonce(cmd, **kwargs):
        if "--result-file" in cmd:
            with open(cmd[cmd.index("--result-file") + 1], "w", encoding="utf-8") as f:
                json.dump({"exit": 0, "reason": "delivered_verified", "landed": True, "nonce": "WRONG", "thread": cc.id}, f)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap), \
         mock.patch("codex_picker_adapter.verify_target", return_value=types.SimpleNamespace(reason=None)), \
         mock.patch("subprocess.run", side_effect=fake_run_wrongnonce):
        code, p = picker.commit_codex(cc, "hi", dry_run=False, expected_cwd="F:/w", registry_path="F:/pinned/reg.json")
    ok(code == 0 and "landed_hint" not in p and p.get("reason") != "delivered_verified",
       "codex 机器结果nonce不归属本次 → 不采信landed/reason（GPT第三轮#1）")
    # porta 选中空cwd＋快照新目录 → cwd_drift 6 零POST（GPT第三轮#2）
    cp_empty = C("porta", "conv1", "antigravity", cwd="")
    fresh_newcwd = C("porta", "conv1", "antigravity", cwd="fixture/new-workspace")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_newcwd])), \
         mock.patch.object(pa.PortaAdapter, "deliver", side_effect=AssertionError("must not deliver")) as md:
        code, p = picker.dispatch_for_slot(candidate=cp_empty, text="hi", dry_run=False)
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "cwd_drift" and md.call_count == 0,
       "porta 选中空cwd快照新目录 → 6 cwd_drift 零POST（GPT第三轮#2）")
    # porta 异host空结果缺席 → 2 porta_host_uncovered（GPT第三轮#3）
    er_alt = types.SimpleNamespace(ok=True, candidates=[], error="", truncated=False, hostId="alt-profile")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_alt), \
         mock.patch.object(pa.PortaAdapter, "deliver", side_effect=AssertionError("must not deliver")) as md:
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "porta_host_uncovered" and md.call_count == 0,
       "porta 异host空结果缺席 → 2 porta_host_uncovered（GPT第三轮#3）")
    # porta deliver not_found异host → 2 porta_host_uncovered（GPT第三轮#3）
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_ok])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(6, {"ok": False, "stage": "verify_target", "reason": "not_found", "hostId": "alt-profile", "truncated": False})):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "porta_host_uncovered",
       "porta deliver not_found异host → 2 porta_host_uncovered（GPT第三轮#3）")
    # porta 同host完整缺席仍权威6（GPT第三轮#3边界）
    er_same = types.SimpleNamespace(ok=True, candidates=[], error="", truncated=False, hostId="antigravity")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_same), \
         mock.patch.object(pa.PortaAdapter, "deliver", side_effect=AssertionError("must not deliver")):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=False)
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "no_local_record",
       "porta 同host完整缺席 → 仍权威6 no_local_record（GPT第三轮#3边界）")
    # porta 回执cwd非空vs选中空 → 2 post_receipt_mismatch（GPT第三轮#2）
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([C("porta", "conv1", "antigravity", cwd="")])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v, cwd="fixture/new-workspace"))):
        code, p = picker.dispatch_for_slot(candidate=cp_empty, text="hi", dry_run=False)
    ok(code == wp.EXIT_ENV and p.get("reason") == "post_receipt_mismatch" and p.get("landed") is True,
       "porta 选中空cwd回执非空 → 2 post_receipt_mismatch（GPT第三轮#2）")

    print("=== v0.4 终审生产新增反例（GPT 2026-11-23） ===")
    # 显式expected_cwd=""＋候选B＋快照B → 空约束不被B覆盖，cwd_drift 6 零POST
    cp_b2 = C("porta", "conv1", "antigravity", cwd="fixture/B")
    fresh_b2 = C("porta", "conv1", "antigravity", cwd="fixture/B")
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_b2])), \
         mock.patch.object(pa.PortaAdapter, "deliver", side_effect=AssertionError("must not deliver")) as md:
        code, p = picker.dispatch_for_slot(candidate=cp_b2, text="hi", dry_run=False, expected_cwd="")
    ok(code == wp.EXIT_UNQUALIFIED and p.get("reason") == "cwd_drift" and md.call_count == 0,
       "显式expected_cwd空＋候选快照B → 6 cwd_drift 零POST（GPT终审）")
    # expected_cwd=None未提供 → 回落候选cwd，B对B放行
    with mock.patch.object(pa.PortaAdapter, "enumerate", return_value=er_with([fresh_b2])), \
         mock.patch.object(pa.PortaAdapter, "deliver", return_value=(0, dict(rec_v, cwd="fixture/B"))) as md:
        code, p = picker.dispatch_for_slot(candidate=cp_b2, text="hi", dry_run=False, expected_cwd=None)
    ok(code == 0 and md.call_args.kwargs.get("expected_cwd") == "fixture/B" and (p.get("delivery_evidence") or {}).get("verified"),
       "expected_cwd=None未提供 → 回落候选B放行（GPT终审边界）")
    # 干跑缺席异host → 分流2（干跑真投共用triage）
    with mock.patch.object(pa.PortaAdapter, "deliver", return_value=(6, {"ok": False, "stage": "verify_target", "reason": "not_found", "hostId": "alt-profile", "truncated": False})):
        code, p = picker.dispatch_for_slot(candidate=cp, text="hi", dry_run=True)
    ok(code == wp.EXIT_ENV and p.get("reason") == "porta_host_uncovered",
       "干跑缺席异host → 2 porta_host_uncovered（干跑真投共用分流，GPT终审）")

    if FAILS:
        print("FAILURES:", len(FAILS))
        sys.exit(1)
    print("DISPATCH_FOR_SLOT V2 TESTS ALL PASS")
