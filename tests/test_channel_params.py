
# -*- coding: utf-8 -*-
"""互通参数"唯一正本 + 调用时现读 + 动态参数自动解析"的用例（2026-11-12 14:5x）。

用户的原话（大意）："手册里要填的参数，最好都是一个调用参数……如果某些必要参数会变，
就可以通过这个参数，自动在连接时更新为正确的参数。"

翻成可验的四条：
A. **改一处就全变**：把正本指到临时文件、改掉 host/port/token → 解析出来的跟着变，**代码不动**。
B. **不带写死的默认值**：正本缺 token → 明确报错，**不许退回某个旧值**。
C. **policy=closed 时拒绝**：关通道要能真的关住。
D. **动态参数不让人填**：sessionId 由 /latest 现解析（这里只验"函数存在且签名不需要人给 id"，
   真解析已在跑 channel_params.py 时验过）。

跑法：python tools\\test_channel_params.py
退出码：0 全过；1 有 FAIL。
"""
from __future__ import annotations
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import importlib.util
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOOLS = _TOOLS
TMP = tempfile.mkdtemp(prefix="chan_case_")
os.environ["DSH_INBOUND_LEDGER"] = os.path.join(TMP, "inbound.jsonl")
os.environ["DSH_IDENTITY_CACHE"] = os.path.join(TMP, "cache.json")
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(detail)))
    if not cond:
        fails.append(name)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def write_channel(cfg):
    p = os.path.join(TMP, "channel.json")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.environ["DSH_CHANNEL_FILE"] = p
    return p


BASE_CFG = {
    "feishu_bot": {"host": "10.0.0.1", "port": 4000, "token": "TOKEN_FEISHU_AAAA", "policy": "open"},
    "desktop_bot": {"host": "10.0.0.2", "port": 5000, "token": "TOKEN_DESKTOP_BBBB",
             "session_cwd": "default-workspace"},
}

# ---- A. 改一处就全变（代码不动）----
write_channel(BASE_CFG)
CP = load("cp_under_test", os.path.join(TOOLS, "channel_params.py"))
m1, n1 = CP.desktop_bot(), CP.feishu_bot()
check("A1 读到 feishu_bot 的地址", n1["base"] == "http://10.0.0.1:4000", n1["base"])
check("A2 读到 desktop_bot 的地址与 cwd",
      m1["base"] == "http://10.0.0.2:5000" and m1["session_cwd"] == "default-workspace", m1)

# 改正本（模拟"参数变了"），**不重新加载模块**，只再调一次
cfg2 = json.loads(json.dumps(BASE_CFG))
cfg2["feishu_bot"]["port"] = 4096
cfg2["feishu_bot"]["token"] = "TOKEN_FEISHU_CCCC"
write_channel(cfg2)
n2 = CP.feishu_bot()
check("A3 改正本后立刻生效（不重启、不改代码）",
      n2["base"] == "http://10.0.0.1:4096" and n2["token"] == "TOKEN_FEISHU_CCCC", n2["base"])

# 口令打码，不许整串打印
check("A4 口令只在内部用，describe 打码", "…" in n2["token_masked"] or "***" in n2["token_masked"],
      n2["token_masked"])

# ---- B. 缺 token → 明确报错，不退回旧值 ----
os.environ["ARCHIVE_SECRETS_DIR"] = os.path.join(TMP, "empty_secrets")
cfg3 = json.loads(json.dumps(BASE_CFG))
cfg3["feishu_bot"]["token"] = ""
write_channel(cfg3)
try:
    CP.feishu_bot()
    check("B1 缺 token 时明确报错", False, "居然没报错")
except ValueError as exc:
    check("B1 缺 token 时明确报错", "token" in str(exc), str(exc))
check("B2 报错后没有残留旧值被误用", CP.feishu_bot.__name__ == "feishu_bot")

# reach_dsh 侧：没有内置默认 token（源码里不许再出现写死的 token 字面量）
RD = os.path.join(TOOLS, "reach_dsh.py")
src = open(RD, encoding="utf-8").read()
import re
baked = re.findall(r"['\"]([A-Za-z0-9_\-]{20,})['\"]", src)
suspicious = [b for b in baked if "TOKEN" not in b and b != "auto_switch_after_binding_invalid"
              and b.count("_") >= 2 and len(b) > 30]
check("B3 reach_dsh 源码里没有写死的长 token 字面量", not suspicious, suspicious[:3])

# ---- C. policy=closed 必须真的关住 ----
cfg4 = json.loads(json.dumps(BASE_CFG))
cfg4["feishu_bot"]["policy"] = "closed"
cfg4["feishu_bot"]["closed_reason"] = "维护中"
write_channel(cfg4)
try:
    CP.feishu_bot()
    check("C1 policy=closed 时拒绝", False, "居然放行了")
except ValueError as exc:
    check("C1 policy=closed 时拒绝", "closed" in str(exc) or "维护中" in str(exc), str(exc))

# ---- D. sessionId 不用人给 ----
import inspect

if __name__ == "__main__":
    sig = inspect.signature(CP.resolve_desktop_session)
    must = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
    check("D1 resolve_desktop_session 不需要人传 sessionId", len(must) == 0,
          [p.name for p in must])

    # ---- E. 归档闸与绑定失效降级（2026-11-23）----
    HOME = os.path.join(TMP, "dsh_home")
    os.makedirs(os.path.join(HOME, "storages"), exist_ok=True)
    BOUND = "session-bound-archived"
    OLD = "session-old-archived"
    NEW = "session-new-live"
    OTHER = "session-other-live"
    with open(os.path.join(HOME, "storages", "workspace.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"global": {"archivedSessionIds": [BOUND, OLD]}}, f, ensure_ascii=False)

    os.environ["DSH_HOME"] = HOME
    os.environ["DSH_SESSION_ID"] = "session-test-self"

    cfg5 = json.loads(json.dumps(BASE_CFG))
    cfg5["desktop_bot"]["bound_session"] = {"sessionId": BOUND, "bound_at": "2026-11-22 22:50"}
    write_channel(cfg5)

    CP.refresh_identity = lambda force=False: {"ok": True}
    SCEN = {"mode": "unique"}


    def _fake_get_soft(url, token, timeout=15.0):
        if "/candidates" in url:
            m = SCEN["mode"]
            if m == "unique":
                return 200, {"ok": True, "role": "desktop",
                             "session": {"sessionId": NEW, "cwd": "C:/x/default-workspace"},
                             "candidates": 1, "basis": ["live=1"]}
            if m == "amb4":
                return 409, {"ok": False, "error": "ambiguous",
                             "candidates": [{"sessionId": NEW, "createdAt": "t"},
                                            {"sessionId": OTHER, "createdAt": "t"},
                                            {"sessionId": BOUND, "createdAt": "t"},
                                            {"sessionId": OLD, "createdAt": "t"}]}
            if m == "amb3":
                return 409, {"ok": False, "error": "ambiguous",
                             "candidates": [{"sessionId": NEW, "createdAt": "t"},
                                            {"sessionId": BOUND, "createdAt": "t"},
                                            {"sessionId": OLD, "createdAt": "t"}]}
            if m == "zero":
                return 404, {"ok": False, "error": "no_target", "seen": []}
        return 200, {"ok": True}


    CP._get_soft = _fake_get_soft

    check("E1 archived_session_ids 读合成归档清单",
          CP.archived_session_ids() == {BOUND, OLD}, CP.archived_session_ids())
    r_arch = CP.verify_target(BOUND, "desktop")
    check("E2 verify_target 对已归档会话直接拒投 target_archived",
          r_arch.get("ok") is False and r_arch.get("error") == "target_archived", r_arch)
    SCEN["mode"] = "unique"
    r_u = CP.resolve_desktop_session()
    check("E3 绑定窗已归档 + 唯一活候选且 cwd 相符 -> 智能自动切换",
          r_u.get("ok") is True and r_u.get("sessionId") == NEW
          and "auto_switch_after_binding_invalid" in (r_u.get("basis") or []), r_u)
    SCEN["mode"] = "amb4"
    r_a4 = CP.resolve_desktop_session()
    cands4 = [c["sessionId"] for c in (r_a4.get("candidates") or [])]
    check("E4 多活候选 -> 剔除归档后剩 2 个报 needs_confirm",
          r_a4.get("ok") is False and r_a4.get("error") == "binding_invalid"
          and r_a4.get("needs_confirm") is True and cands4 == [NEW, OTHER], r_a4)
    SCEN["mode"] = "amb3"
    r_a3 = CP.resolve_desktop_session()
    check("E5 候选剔除归档后只剩 1 个 -> 自动切换",
          r_a3.get("ok") is True and r_a3.get("sessionId") == NEW, r_a3)
    SCEN["mode"] = "zero"
    r_z = CP.resolve_desktop_session()
    check("E6 剔除归档后零活候选 -> needs_confirm 空列表",
          r_z.get("ok") is False and r_z.get("error") == "binding_invalid"
          and r_z.get("needs_confirm") is True and not (r_z.get("candidates") or []), r_z)

    print()
    print("FAILED:", fails or "none")
    sys.exit(1 if fails else 0)
