# -*- coding: utf-8 -*-
"""picker.py — Unified window-picker orchestrator (three legs -> one card).

Wires the per-leg adapters (codex_picker_adapter / porta_adapter; harbor deferred)
into the leg-agnostic core (window_picker): gather -> select/keyword -> card -> dispatch.
Single-writer integration by desktop_bot from GPT (codex) / review_bot (Porta) tested drafts.

Design: 内部设计文档（略）. Core invariant: NEVER guess.
Read-only by default; dispatch dry_run=True unless explicitly armed.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import window_picker as wp
import paths

CORE_FIELDS = [f.name for f in dataclasses.fields(wp.UnifiedCandidate)]
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
GPT_REG = paths.codex_registry_path(for_write=True)

# 受保护工作区硬隔离（红线）：复用 codex_picker_adapter 单一真相源（与 reach_gpt 提交前同口径）
LOCAL_HOST_ID = "local"  # 受信任的本机 codex 存储 host；异 host 由 verify_target 判 host_mismatch


def _workspace_allowed(addr, cwd: str) -> bool:
    import codex_picker_adapter as cxa
    return cxa.default_workspace_allowed(addr, cwd)


def _core_from_dict(row: dict) -> wp.UnifiedCandidate:
    return wp.UnifiedCandidate(**{k: row.get(k) for k in CORE_FIELDS})


def _core_from_obj(obj) -> wp.UnifiedCandidate:
    return wp.UnifiedCandidate(**{k: getattr(obj, k) for k in CORE_FIELDS})


def enumerate_codex(registry_path=None) -> tuple[list, bool, str]:
    reg = Path(registry_path) if registry_path else GPT_REG
    try:
        import codex_picker_adapter as cxa
    except Exception as exc:  # noqa: BLE001
        return [], False, "codex_adapter_import:%s" % type(exc).__name__
    try:
        res = cxa.enumerate(host_id="local", codex_home=CODEX_HOME, registry_path=reg,
                            workspace_allowed=_workspace_allowed)
    except Exception as exc:  # noqa: BLE001
        return [], False, "codex_enumerate:%s" % type(exc).__name__
    err = str(res.get("error") or "")
    if not err and not res.get("ok", True):
        err = "codex_degraded"
    return [_core_from_dict(r) for r in res.get("candidates", [])], bool(res.get("truncated")), err


def enumerate_porta() -> tuple[list, bool, str, str]:
    """返回 (rows, truncated, error, host_id)——host 取自提供列表的同一次 EnumerateResult
    （GPT桥复审#5：不得借第二次健康检查把 A 的空结果标成覆盖 B）。"""
    try:
        import porta_adapter as pa
    except Exception as exc:  # noqa: BLE001
        return [], False, "porta_adapter_import:%s" % type(exc).__name__, ""
    res = pa.PortaAdapter().enumerate()
    if not res.ok:
        return [], False, res.error or "porta_unhealthy", getattr(res, "hostId", "") or ""
    return [_core_from_obj(c) for c in res.candidates], res.truncated, "", res.hostId or ""


def enumerate_harbor() -> tuple[list, bool, str]:
    # harbor title source deferred to DSH-restart batch (v1.0 §12); stub for now.
    return [], False, "harbor_deferred"


def gather_with_status(registry_path=None) -> dict:
    """全腿枚举＋腿级状态（调度层契约v0.3 §一.3，GPT定稿语义，2026-11-22）。
    返回 {"candidates": [UnifiedCandidate...], "legs": {leg: {ok, truncated, error, hosts, roles, stub?}}}。
    权威缺失判定：仅当目标腿 ok ∧ ¬truncated ∧ error空 ∧ 非stub/disabled ∧ hosts/roles 覆盖绑定范围，
    目标缺席才可标 stale；否则调用方须保留槽位给 need_evidence。harbor 枚举未实现＝stub（重启批）。"""
    legs: dict = {}
    cands: list = []
    # codex：可注入登记表（编排层 strict registry 契约）；默认 GPT_REG。走 enumerate_* 保持可mock。
    try:
        if registry_path:
            rows, trunc, err = enumerate_codex(registry_path=registry_path)  # pin 贯穿，绝不回落默认册（GPT复审#4）
        else:
            rows, trunc, err = enumerate_codex()
        ok = not err
        legs["codex"] = {"ok": ok, "truncated": bool(trunc), "error": err or "",
                         "hosts": ["local"] if ok else [], "roles": ["peer"] if ok else []}
        cands.extend(rows)
    except Exception as exc:  # noqa: BLE001
        legs["codex"] = {"ok": False, "truncated": False,
                         "error": "codex_enumerate:%s:%s" % (type(exc).__name__, exc),
                         "hosts": [], "roles": []}
    try:
        rows, trunc, err, enum_host = enumerate_porta()
        ok = not err
        # 覆盖只认提供列表的同一次枚举元数据（GPT桥复审#5）：空结果＝该host权威空，不借二次健康检查
        hosts = [enum_host] if (ok and enum_host) else sorted({c.hostId for c in rows if getattr(c, "hostId", "")})
        legs["porta"] = {"ok": ok, "truncated": bool(trunc), "error": err or "",
                         "hosts": hosts if ok else [], "roles": ["peer"] if ok else []}
        cands.extend(rows)
    except Exception as exc:  # noqa: BLE001
        legs["porta"] = {"ok": False, "truncated": False,
                         "error": "porta_enumerate:%s:%s" % (type(exc).__name__, exc),
                         "hosts": [], "roles": []}
    # harbor 标题源待 DSH 重启批（v1.0 §12）：stub＝未查询，不得据缺席标 stale
    rows, trunc, err = enumerate_harbor()
    legs["harbor"] = {"ok": not err, "truncated": bool(trunc), "error": err or "",
                      "stub": bool(err), "hosts": [], "roles": []}
    cands.extend(rows)
    return {"candidates": wp._dedup(cands), "legs": legs}


def gather() -> list:
    """兼容包装：只要候选列表（旧调用方行为不变）。"""
    return gather_with_status()["candidates"]


def pick(keyword: str = "", explicit: str = "") -> tuple[int, object]:
    return wp.select(gather(), explicit=explicit, keyword=keyword)


def card(page: int = 0) -> dict:
    return wp.build_card(gather(), page=page)


def card_grouped(scroll_cap: int = 60) -> dict:
    """路 A 真滚动分组卡（镜像平台排列＋分组），全腿 gather 转核心候选后渲染。"""
    return wp.build_card_grouped(gather(), scroll_cap=scroll_cap)


def dispatch(candidate: wp.UnifiedCandidate, text: str, peer_agent: str = "desktop_bot",
             dry_run: bool = True) -> tuple[int, dict]:
    """Route a selected candidate to its leg. dry_run=True reports the route only."""
    if candidate.leg == "porta":
        import porta_adapter as pa
        return pa.PortaAdapter().deliver(candidate.id, text, peer_agent=peer_agent, dry_run=dry_run)
    if candidate.leg == "codex":
        # GPT verify 清单 #5/#6：统一调用层接工作区授权＋提交前用新快照复验同一键
        import codex_picker_adapter as cxa
        key = ("codex", candidate.hostId, candidate.id)
        try:
            # 按受信任的本机 host 读快照（异 host 由 verify_target 判 host_mismatch，GPT 清单 #5）
            snap = cxa.read_snapshot(host_id=LOCAL_HOST_ID, codex_home=CODEX_HOME, registry_path=GPT_REG)
            verdict = cxa.verify_target(snap, key, workspace_allowed=_workspace_allowed)
        except Exception as exc:  # noqa: BLE001
            return (wp.EXIT_ENV, {"ok": False, "leg": "codex",
                                  "reason": "reverify_error:%s" % type(exc).__name__})
        if verdict.reason is not None:
            return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "codex", "stage": "pre_commit_reverify",
                                          "reason": verdict.reason})
        # 比对选中与当前的规范 cwd（选中后漂移即拒，需重新点名；GPT 清单 #5）
        import urllib.parse as _up
        cur_cwd = cxa.fs_path_key(snap.threads.get(key, {}).get("cwd") or "")
        sel_cwd = cxa.fs_path_key(candidate.cwd or "")
        if cur_cwd != sel_cwd:
            return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "codex", "stage": "pre_commit_reverify",
                                          "reason": "cwd_drift"})
        route = ["tools/send_codex.py", "--thread", candidate.id, "--confirmed", "--file", "<正文文件>"]
        return (wp.EXIT_OK, {"ok": True, "dry_run": dry_run, "leg": "codex", "verified": True,
                             "route": route, "note": "pre-commit re-verify passed; deliver via send_codex"})
    if candidate.leg == "harbor":
        route = ["reach_dsh.py", "--session", candidate.id, "--file", "<正文文件>"]
        return (wp.EXIT_OK, {"ok": True, "dry_run": dry_run, "leg": "harbor",
                             "route": route, "note": "harbor deliver via reach_dsh (title deferred)"})
    return (wp.EXIT_ENV, {"ok": False, "reason": "unknown_leg", "leg": candidate.leg})

def commit_codex(candidate: wp.UnifiedCandidate, text: str, dry_run: bool = True,
                 expected_cwd=None, registry_path=None) -> tuple[int, dict]:
    """经统一投递入口提交已复验的 Codex 地址。

    选中地址经 --thread 传入；reach_gpt 在实际提交前（:566）复用同一
    codex_picker_adapter.default_workspace_allowed 工作区授权＋cwd 漂移校验。
    dry_run=True 走 reach_gpt --dry-run（只读、不真投、不耗额度），验证整条授权链。
    调用前应先经 dispatch 的提交前复验（受信任 host＋资格＋cwd 一致）。
    """
    import json as _json
    import os
    import subprocess
    import sys
    import tempfile
    import urllib.parse as _up
    import uuid
    import codex_picker_adapter as cxa
    # 发送入口拿「选窗时的工作区」：提交前按受信任 host 复验资格＋比对选中 cwd 与当前 cwd
    key = ("codex", candidate.hostId, candidate.id)
    reg = Path(registry_path) if registry_path else GPT_REG
    try:
        snap = cxa.read_snapshot(host_id=LOCAL_HOST_ID, codex_home=CODEX_HOME, registry_path=reg)
        verdict = cxa.verify_target(snap, key, workspace_allowed=_workspace_allowed)
    except Exception as exc:  # noqa: BLE001
        return (wp.EXIT_ENV, {"ok": False, "leg": "codex", "reason": "commit_reverify_error:%s" % type(exc).__name__})
    if verdict.reason is not None:
        return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "codex", "stage": "commit_reverify", "reason": verdict.reason})
    cur_cwd = cxa.fs_path_key(snap.threads.get(key, {}).get("cwd") or "")
    sel_cwd = cxa.fs_path_key(expected_cwd if expected_cwd is not None else (candidate.cwd or ""))
    if cur_cwd != sel_cwd:
        return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "codex", "stage": "commit_reverify",
                                      "reason": "cwd_drift", "note": "选窗时工作区与发送入口当前不符"})
    reach = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_codex.py")
    tmp = None
    result_tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
            tf.write(text)
            tmp = tf.name
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as rf:
            result_tmp = rf.name
        nonce = uuid.uuid4().hex
        cmd = [sys.executable, reach, "--thread", candidate.id, "--confirmed", "--file", tmp,
               "--nonce", nonce, "--registry", str(reg),  # pinned 登记册贯穿桥子进程（GPT复审#4）
               "--result-file", result_tmp]  # 机器结果独立通道（GPT桥复审#5：回复文本不可污染状态）
        eff_cwd = expected_cwd if expected_cwd is not None else candidate.cwd
        if eff_cwd is not None:
            cmd += ["--expected-cwd", eff_cwd]  # 显式空cwd也传发送入口复验（None=未提供才省略，GPT终审）
        cmd += ["--dry-run"] if dry_run else ["--mode", "queue"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
            rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            # 晚到结果：桥可能已投达（queue 判达轮询超父进程预算）；nonce 回执仍可归属本次（GPT复审#5）
            partial = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            return (wp.EXIT_ENV, {"ok": False, "leg": "codex", "dry_run": dry_run, "reason": "commit_timeout",
                                  "timeout": True, "nonce": nonce, "exit": None, "output": (partial or "")[-800:]})
        # 机器状态只读结构化结果文件，绝不从混有回复的stdout取REASON（GPT桥复审#5）
        machine = {}
        try:
            with open(result_tmp, "r", encoding="utf-8") as fh:
                machine = _json.load(fh)
            if not isinstance(machine, dict):
                machine = {}
        except Exception:  # noqa: BLE001
            machine = {}
        # 机器结果按 nonce/thread/exit 校验归属本次（GPT第三轮#1）：不匹配不采信
        if (machine.get("nonce") != nonce or machine.get("thread") != candidate.id
                or machine.get("exit") != rc):
            machine = {}
        reason = str(machine.get("reason") or "")
        payload = {"ok": rc == 0, "leg": "codex", "dry_run": dry_run, "exit": rc,
                   "nonce": nonce, "output": out[-800:],
                   "note": "已选地址经统一入口提交，保留发送前工作区复验"}
        if isinstance(machine.get("landed"), bool):
            payload["landed_hint"] = machine["landed"]
        if "resultKnown" in machine:
            payload.update(status=machine.get("status"), resultKnown=machine["resultKnown"])
        if reason:
            payload["reason"] = reason  # 结构化拒绝：编排层按 reason 分流权威/瞬时
        elif rc != 0:
            payload["reason"] = "bridge_exit_%d" % rc  # 结果文件缺失只给类别，不解析文本
        return (rc, payload)
    except Exception as exc:  # noqa: BLE001
        return (wp.EXIT_ENV, {"ok": False, "leg": "codex", "reason": "commit_error:%s" % type(exc).__name__})
    finally:
        for p in (tmp, result_tmp):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def dispatch_for_slot(*, candidate: wp.UnifiedCandidate, text: str, dry_run: bool = True,
                      expected_cwd=None, registry_path=None,
                      peer_agent: str = "desktop_bot") -> tuple[int, dict]:
    """编排层 dispatch_fn 关键字契约（调度层契约v0.3 §一.4/8，2026-11-22）。

    各腿真投＋可核验 DeliveryEvidence（payload["delivery_evidence"]，仅真判达时出现）：
    - porta：deliver 正文指纹＋step增长判达（stage verified/delivered_running ∧ matchedBodyFingerprint）
      → evidence_type=porta_verified_step，message_id=clientMessageId。
    - codex：commit_codex 真投 → 读 reach_gpt 回执（与 reach_gpt:110 同一 resolve 口径）「正文进了」=True
      → evidence_type=codex_reach_gpt_verified，message_id=回执引用（at＋thread 唯一定位该记录）。
      exit5＋已landed＝送达在跑（勿重投），归一为 ok；exit5＋未landed＝瞬时失败（编排层 need_evidence）。
    - harbor：枚举/回执核验待重启批 → 拒投（stub），绝不假报送达。
    路线预览/dry_run/仅受理/失败一律不产 verified 证据（内部守则（略）：绑定只认可核验判达）。
    """
    import datetime as _dt
    import json as _json
    import time as _time
    leg = candidate.leg
    if leg == "porta":
        import porta_adapter as pa
        tkey = ["porta", candidate.hostId, candidate.id]
        adapter = pa.PortaAdapter()
        sel_role = (candidate.role or "").strip()
        # None=未提供约束（回落候选cwd）；""=显式选窗空约束（参加比较，GPT终审）
        eff_cwd = expected_cwd if expected_cwd is not None else (candidate.cwd or "")
        if not dry_run:
            # 早期拒绝（省一次POST尝试）；权威终验在 deliver 内 POST 时刻同一快照（GPT桥复审#2）
            try:
                er = adapter.enumerate()
            except Exception as exc:  # noqa: BLE001
                return (wp.EXIT_ENV, {"ok": False, "leg": "porta", "target_key": tkey,
                                      "reason": "reverify_error:%s" % type(exc).__name__})
            if not er.ok:
                return (wp.EXIT_ENV, {"ok": False, "leg": "porta", "target_key": tkey,
                                      "reason": "porta_unhealthy", "detail": er.error or ""})
            fresh = next((c for c in er.candidates if c.id == candidate.id), None)
            if fresh is None:
                if er.truncated:
                    # 截断缺席非权威（GPT桥复审#5）：待补证据，不得标 stale
                    return (wp.EXIT_ENV, {"ok": False, "leg": "porta", "target_key": tkey,
                                          "reason": "porta_truncated", "note": "枚举截断，缺席不权威"})
                if er.hostId and candidate.hostId and er.hostId != candidate.hostId:
                    # 异host空结果不覆盖已选host，缺席不权威（GPT第三轮#3）：待补证据
                    return (wp.EXIT_ENV, {"ok": False, "leg": "porta", "target_key": tkey,
                                          "reason": "porta_host_uncovered", "enum_host": er.hostId,
                                          "note": "枚举只覆盖其他host，未覆盖已选host，缺席不权威"})
                return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "porta", "target_key": tkey,
                                              "reason": "no_local_record"})
            if fresh.hostId != candidate.hostId:
                return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "porta", "target_key": tkey,
                                              "reason": "host_mismatch", "fresh_host": fresh.hostId})
            if fresh.unusableReason:
                ur = str(fresh.unusableReason)
                if ur.startswith("unknown_status"):
                    # 身份/状态未知＝缺证据（GPT桥复审#5），非权威资格变更
                    return (wp.EXIT_ENV, {"ok": False, "leg": "porta", "target_key": tkey,
                                          "reason": "identity_missing", "detail": ur})
                return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "porta", "target_key": tkey,
                                              "reason": ur})
            fresh_role = (fresh.role or "").strip()
            if fresh_role in ("", "unknown") or sel_role in ("", "unknown"):
                return (wp.EXIT_ENV, {"ok": False, "leg": "porta", "target_key": tkey,
                                      "reason": "identity_missing", "fresh_role": fresh.role})
            if sel_role != "peer" or fresh_role != sel_role:
                return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "porta", "target_key": tkey,
                                              "reason": "role_mismatch", "fresh_role": fresh.role})
            # 显式空cwd也参加比较（选中空cwd≠无约束，GPT第三轮#2）
            import codex_picker_adapter as cxa
            if cxa.fs_path_key(fresh.cwd or "") != cxa.fs_path_key(eff_cwd):
                return (wp.EXIT_UNQUALIFIED, {"ok": False, "leg": "porta", "target_key": tkey,
                                              "reason": "cwd_drift"})
        # 完整选中键/角色/选窗cwd传入deliver的POST时刻终验（dry-run同闸，GPT桥复审#2）
        code, payload = adapter.deliver(candidate.id, text, peer_agent=peer_agent, dry_run=dry_run,
                                        expected_host=candidate.hostId, expected_role=sel_role,
                                        expected_cwd=eff_cwd)
        payload = dict(payload or {})
        payload["target_key"] = tkey
        # deliver内终验/复验拒绝分流（干跑真投共用，GPT终审）：截断/异host缺席/身份未知→2待核；明确资格变更→6
        if not payload.get("ok") and payload.get("stage") in ("verify_target", "final_reverify"):
            rsn = str(payload.get("reason") or "")
            if payload.get("truncated") and rsn in ("not_found", "no_local_record"):
                payload["reason"] = "porta_truncated"
                return (wp.EXIT_ENV, payload)
            if (payload.get("hostId") and candidate.hostId and payload["hostId"] != candidate.hostId
                    and rsn in ("not_found", "no_local_record")):
                payload["reason"] = "porta_host_uncovered"
                return (wp.EXIT_ENV, payload)
            if rsn.startswith("unknown_status") or rsn == "identity_missing":
                payload["reason"] = "identity_missing"
                return (wp.EXIT_ENV, payload)
            return code, payload
        if dry_run:
            return code, payload
        # 回执目标按实测核对（GPT桥复审#2）：host/cwd与选中不符→POST已发生则待核勿重投
        if payload.get("hostId") and payload.get("hostId") != candidate.hostId:
            payload.update({"ok": False, "landed": True, "reason": "post_receipt_mismatch",
                            "note": "回执host与选中不符，勿重投；人工核验"})
            return (wp.EXIT_ENV, payload)
        if "cwd" in payload:
            import codex_picker_adapter as cxa
            if cxa.fs_path_key(payload.get("cwd") or "") != cxa.fs_path_key(eff_cwd):
                payload.update({"ok": False, "landed": True, "reason": "post_receipt_mismatch",
                                "note": "回执cwd与选窗不符，勿重投；人工核验"})
                return (wp.EXIT_ENV, payload)
        if (code == wp.EXIT_OK and payload.get("ok")
                and payload.get("matchedBodyFingerprint")
                and payload.get("matchedClientMessageId")
                and payload.get("stage") in ("verified", "delivered_running")
                and payload.get("clientMessageId")):
            payload["delivery_evidence"] = {
                "leg": "porta", "target_key": tkey,
                "evidence_type": "porta_verified_step",
                "message_id": str(payload["clientMessageId"]),
                "delivered_at": str(payload.get("at") or _dt.datetime.now().astimezone().isoformat(timespec="seconds")),
                "verified": True, "dry_run": False, "route_only": False,
            }
            return code, payload
        if code == wp.EXIT_OK and payload.get("matchedBodyFingerprint") and not payload.get("matchedClientMessageId"):
            payload.update({"ok": False, "landed": True, "reason": "porta_step_association_missing",
                            "note": "正文指纹命中但step消息ID未对上，不产证据；勿重投，人工核验"})
            return (wp.EXIT_ENV, payload)
        return code, payload
    if leg == "codex":
        tkey = ["codex", candidate.hostId, candidate.id]
        if dry_run:
            code, payload = commit_codex(candidate, text, dry_run=True,
                                         expected_cwd=expected_cwd, registry_path=registry_path)
            payload = dict(payload or {})
            payload["target_key"] = tkey
            return code, payload
        code, payload = commit_codex(candidate, text, dry_run=False,
                                     expected_cwd=expected_cwd, registry_path=registry_path)
        payload = dict(payload or {})
        payload["target_key"] = tkey
        # 权威拒绝（exit6 资格/漂移，REASON 结构化）原样上抛：编排层据此标 stale（GPT复审#5）
        if code == wp.EXIT_UNQUALIFIED:
            payload.setdefault("reason", "unqualified")
            return code, payload
        # 忙态只消费受控机器状态（GPT桥复审#5）：exit7或非0＋机器reason；成功0绝不被回复文本翻成7
        if code == 7 or (payload.get("reason") == "busy_window" and code != wp.EXIT_OK):
            payload["ok"] = False
            payload["reason"] = "busy_window"
            return 7, payload
        # 判达归属本次提交：只认本次 nonce 的回执（同秒旧回执/同前缀旧正文一律不归属，GPT复审#1）
        nonce = str(payload.get("nonce") or "")
        entry = None
        if nonce:
            receipt_path = paths.state_path("codex_receipt.json", for_write=True)
            try:
                with open(receipt_path, "r", encoding="utf-8") as fh:
                    rows = _json.load(fh)
                hits = [r for r in rows if isinstance(r, dict) and r.get("nonce") == nonce
                        and r.get("thread") == candidate.id]
                if hits:
                    entry = max(hits, key=lambda r: str(r.get("at") or ""))
            except Exception:  # noqa: BLE001
                entry = None
        receipt_landed = bool(entry and entry.get("正文进了"))
        if receipt_landed:
            at = str(entry.get("at") or "")
            try:
                delivered_at = _dt.datetime.strptime(at, "%Y-%m-%d %H:%M:%S").astimezone().isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                delivered_at = at
            payload.update({
                "ok": True, "landed": True, "exit": code,
                "delivery_evidence": {
                    "leg": "codex", "target_key": tkey,
                    "evidence_type": "codex_reach_gpt_verified",
                    "message_id": "gpt-receipt:%s" % nonce,
                    "delivered_at": delivered_at,
                    "verified": True, "dry_run": False, "route_only": False,
                },
            })
            if code != wp.EXIT_OK:
                payload["note"] = "正文已确认进rollout、GPT在跑（exit%s）；勿重投，--tail观察终态" % code
            return wp.EXIT_OK, payload
        # 缺回执：按校验过的机器结果保留待核/勿重投，绝不从stdout判landed（GPT第三轮#1）
        if payload.get("landed_hint") is True:
            payload.update({"ok": False, "landed": True, "reason": "codex_receipt_missing",
                            "note": "机器结果确认正文已落盘但无本次nonce回执，勿重投；人工 --tail 核验"})
            return wp.EXIT_ENV, payload
        payload.update({"ok": False, "landed": False})
        if payload.get("timeout"):
            payload["reason"] = "commit_timeout"  # 晚到结果未知：瞬时→need_evidence（GPT复审#5）
        else:
            payload.setdefault("reason", "codex_not_landed")
        return (code if code not in (wp.EXIT_OK, None) else wp.EXIT_ENV), payload
    if leg == "harbor":
        return (wp.EXIT_ENV, {"ok": False, "leg": "harbor", "reason": "harbor_delivery_stub",
                              "target_key": [leg, candidate.hostId, candidate.id],
                              "note": "harbor枚举与回执核验待DSH重启批；不假报送达（need_evidence）"})
    return (wp.EXIT_ENV, {"ok": False, "reason": "unknown_leg", "leg": leg,
                          "target_key": [leg, candidate.hostId, candidate.id]})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="统一选窗器（只读枚举/选窗/卡片；投递默认 dry_run）")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--explicit", default="")
    ap.add_argument("--card", action="store_true")
    ap.add_argument("--page", type=int, default=0)
    args = ap.parse_args()
    if args.card:
        c = card(args.page)
        print("活跃 %d 个，第 %d/%d 页" % (c["total"], c["page"] + 1, c["pages"]))
        for o in c["question"]["options"]:
            print("  - %s | %s" % (o["label"], o.get("description", "")))
    else:
        code, payload = pick(keyword=args.keyword, explicit=args.explicit)
        print("exit=%d" % code)
        if isinstance(payload, list):
            for c in payload:
                print("  [%s] %s (%s) %s" % (c.leg, c.title, c.id[:8], c.unusableReason or ""))
        elif isinstance(payload, wp.UnifiedCandidate):
            print("  唯一合格: [%s] %s (%s)" % (payload.leg, payload.title, payload.id))
        else:
            print("  %s" % payload)
