# -*- coding: utf-8 -*-
r"""互通参数的**唯一解析处**（2026-11-12 14:5x，用户的要求）。

用户原话（大意）：
    "标准手册中要填的参数，最好都是一个调用参数……像调用函数一样，
     如果因为各种原因某些必要参数会变，就可以通过这个参数，自动在连接时更新为正确的参数。"

翻成规矩，就三条：

**一、参数只存在于一个地方：`工作台\通道.json`。**
    任何工具都不许把 host / port / token **写死在代码里当默认值**——
    写死的那一份就是"会变但没人更新"的定时炸弹（历史：token 抄在三处，一改就 401，且没人报）。

**二、调用时现读，不缓存。**
    每次真要连的时候才读一遍正本；改了正本立刻生效，不用改代码、不用重启工具。

**三、会变的参数不让人填，自动解析。**
    - `sessionId`（换新对话就变）→ **按身份解析**（2026-11-17 起，不再用 /latest）：
      会话是谁由 session_identity 判、活着没有由插件判，唯一才投、歧义就拒；
    - 端口 / 口令 → 从正本读；
    - 于是调用方**只需要给"要说什么"**这一个东西。
    - 历史教训：`/latest` 只按活跃时间挑，10:47 返回飞书助手、11:2x 返回桌面助手，
      同一条命令两个结果——**它是查询，不是投递依据**。

用法（别自己抄地址和口令，一律用它）：
    from channel_params import desktop_bot, feishu_bot, resolve_desktop_session, resolve_feishu_session
    m = desktop_bot()                              # {'base','host','port','token','session_cwd'}
    n = feishu_bot()                              # {'base','host','port','token','wake', ...}
    r = resolve_desktop_session()           # 显式 > 一次性绑定 > 唯一合格；否则报错
    q = resolve_feishu_session()            # bot state 的 p2p 绑定 + 身份校验

自检：`python tools\channel_params.py` 会把两边解析出来的参数与目标解析结果打出来
      （不打印完整口令）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHANNEL = os.environ.get("DSH_CHANNEL_FILE") or str(paths.state_path("通道.json", for_write=False))

# 来件台账：插件受理投递时写的一行 JSONL（{at,to,from,fromRole,requestId,mode}）。
# 回话时先用它找回"这条消息是谁从哪个窗口发来的"，再复验那个窗口。
INBOUND_LEDGER = os.environ.get("DSH_INBOUND_LEDGER") or str(paths.state_path("来件关联.jsonl", for_write=True))
ORIGIN_TTL_MINUTES = 30


def _fill_tokens(cfg: dict) -> dict:
    """口令库外：正本只留 token_secret 指针，现读 secrets_dir 合并（不缓存）。"""
    sec = paths.secrets_dir()
    for name, defkey in (("feishu_bot", "harbor_feishu"), ("desktop_bot", "harbor_desktop")):
        s = cfg.get(name) or {}
        if not s.get("token"):
            p = sec / ((s.get("token_secret") or defkey) + ".txt")
            if p.is_file():
                s["token"] = p.read_text(encoding="utf-8").strip()
        cfg[name] = s
    m = cfg.get("desktop_bot") or {}
    if not m.get("feishu_reply_token"):
        p = sec / ((m.get("feishu_reply_token_secret") or "harbor_feishu_reply") + ".txt")
        if p.is_file():
            m["feishu_reply_token"] = p.read_text(encoding="utf-8").strip()
    return cfg


def load() -> dict:
    """读正本。**每次都现读**——不缓存，改了就生效。口令从库外合并。"""
    path = os.environ.get("DSH_CHANNEL_FILE", CHANNEL)
    with open(path, encoding="utf-8-sig") as f:
        return _fill_tokens(json.load(f) or {})


def _mask(token: str) -> str:
    t = str(token or "")
    return (t[:6] + "…" + t[-4:]) if len(t) > 12 else ("***" if t else "")


def _side(cfg: dict, name: str) -> dict:
    s = (cfg or {}).get(name) or {}
    host = str(s.get("host") or "")
    port = str(s.get("port") or "")
    token = str(s.get("token") or "")
    if not (host and port):
        raise ValueError("通道.json 里 %s 段的 host/port 缺失" % name)
    if not token:
        raise ValueError("通道.json 里 %s 段的 token 缺失" % name)
    policy = str(s.get("policy") or "open").lower()
    if policy != "open":
        raise ValueError("通道被正本 policy=%s 关着：%s"
                         % (policy, s.get("closed_reason") or "（没写理由）"))
    return {
        "base": "http://%s:%s" % (host, port),
        "host": host, "port": port, "token": token,
        "token_masked": _mask(token),
        "session_cwd": str(s.get("session_cwd") or ""),
        "raw": s,
    }


def desktop_bot() -> dict:
    """桌面助手侧（她的港湾 = 飞书助手投给桌面助手的入口）。"""
    return _side(load(), "desktop_bot")


def feishu_bot() -> dict:
    """飞书助手侧（回波 = 桌面助手叫醒飞书助手的入口）。"""
    return _side(load(), "feishu_bot")


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, token: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"X-Harbor-Token": token}, method="GET")
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def post_json(url: str, token: str, payload: dict, timeout: float = 20.0):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "X-Harbor-Token": token}, method="POST")
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def _plugin_absent(body: dict) -> bool:
    """404 到底是"插件没加载"还是"业务上没这个目标"。

    插件没加载时宿主回的是通用 404（没有业务 error 字段）；业务拒绝会带
    no_target / ambiguous 这类 error。只看状态码会把两者混成一件。
    """
    err = str((body or {}).get("error") or "").strip().lower()
    return err in ("", "not_found", "not found", "http 404", "http404")


def _get_soft(url: str, token: str, timeout: float = 15.0):
    """像 _get，但 4xx/5xx 也把 JSON 正文读回来（业务拒绝的原因在里面）。"""
    try:
        return _get(url, token, timeout)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            body = {"ok": False, "error": "HTTP %s" % exc.code}
        if not isinstance(body, dict):
            body = {"ok": False, "error": "HTTP %s" % exc.code}
        body.setdefault("http", exc.code)
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def post_json_soft(url: str, token: str, payload: dict, timeout: float = 20.0):
    """像 post_json，但 HTTP 200 不等于成功：把业务 ok/accepted 一起交给调用方判。"""
    try:
        status, body = post_json(url, token, payload, timeout)
        return status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            body = {"ok": False, "error": "HTTP %s" % exc.code}
        body.setdefault("http", exc.code)
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------- 身份与目标解析
# 2026-11-17 首批修复（A01）：**不再用 /latest 找投递对象**。
# 身份由 session_identity.py 判（读原始会话头与预设事件），live 由插件判，两边不重复也不互猜。

def refresh_identity(force: bool = False) -> dict:
    """刷新身份缓存（增量）。投递前必须调一次——插件按缓存年龄判过期。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import session_identity  # noqa: E402

    return session_identity.refresh(force=force)


def identity_role(session_id: str) -> str:
    """**缓存观察**：只说明缓存那一刻它像谁，不等于现在还能投（投递请用 verify_target）。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import session_identity  # noqa: E402

    row = session_identity.lookup(session_id)
    return (row or {}).get("role") or "unknown"


def verify_target(session_id: str, expect: str = "", side: dict | None = None) -> dict:
    """投递前的统一复验（2026-11-17 R1/R3）：先刷新缓存，再让插件按**当前头 + 活/已持久化**判。

    为什么不能只读缓存：缓存合格只说明"生成缓存那一刻它像谁"。目标可能已经关了、
    或者被换成了另一个预设；这两种插件才看得见。所以三条路径（显式／通道绑定／
    正向解析）都必须走这里，谁也不许拿 identity_role() 直接当放行依据。

    冷会话（已持久化、当前不活）也算合格：插件会先 resolveAgent 把它叫醒，再投。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "sessionId_required", "detail": "没有给目标会话"}
    if sid in archived_session_ids():
        return {"ok": False, "http": 410, "error": "target_archived", "sessionId": sid,
                "detail": "目标会话已归档，绝不投递", "basis": ["verify_target"]}
    m = side or desktop_bot()
    try:
        refresh_identity()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "identity_refresh_failed",
                "detail": "%s: %s" % (type(exc).__name__, exc)}
    url = m["base"] + "/api/harbor/target?sessionId=" + urllib.parse.quote(sid)
    if expect:
        url += "&expect=" + urllib.parse.quote(expect)
    status, body = _get_soft(url, m["token"])
    body = body or {}
    if status == 404 and _plugin_absent(body):
        return {"ok": False, "http": 404, "error": "harbor_not_loaded", "sessionId": sid,
                "detail": "港湾插件没加载（/api/harbor/target 返回 404）；"
                          "重启 DSH 让它加载后才能复验与投递",
                "basis": ["verify_target"]}
    if body.get("ok"):
        return {"ok": True, "sessionId": body.get("sessionId") or sid,
                "role": body.get("role") or "unknown",
                "source": body.get("source"),
                "cache_built_at": body.get("cache_built_at"),
                "evidence": body.get("evidence"),
                "live": body.get("live", True),
                "persisted": body.get("persisted", False),
                "http": status, "basis": ["verify_target"]}
    return {"ok": False, "http": status,
            "error": body.get("error") or "unknown",
            "role": body.get("role"),
            "sessionId": sid,
            "detail": body.get("detail") or "",
            "hint": body.get("hint") or "",
            "basis": ["verify_target"]}


def my_session_id() -> str:
    """我这条会话的 id（宿主 shell 里有 DSH_SESSION_ID）。取不到就返回空串，由调用方要 --from。"""
    return os.environ.get("DSH_SESSION_ID", "").strip()


def recent_inbound(to_sid: str = "", within_minutes: int = ORIGIN_TTL_MINUTES) -> dict | None:
    """最近一条**投给我的**来件（台账里 to=我的会话、from 非空）。

    只认 within_minutes 之内的：再久的就不算"回这条消息"了，按新投递重新解析目标，
    免得一条早就关掉的窗口把之后所有回话都卡成 origin_unavailable。
    """
    target = (to_sid or "").strip()
    if not target or not os.path.isfile(INBOUND_LEDGER):
        return None
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    picked = None
    try:
        with open(INBOUND_LEDGER, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if str(row.get("to") or "") != target or not str(row.get("from") or ""):
                    continue
                try:
                    at = datetime.fromisoformat(str(row.get("at") or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if at < cutoff:
                    continue
                picked = row
    except OSError:
        return None
    return picked


def _binding_of(side: dict) -> dict | None:
    """通道正本里登记过的"一次性明确绑定"（说明 §5.2 允许的那种）。"""
    raw = (side.get("raw") or {}).get("bound_session")
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("sessionId") or "").strip()
    if not sid:
        return None
    return {"sessionId": sid,
            "bound_at": raw.get("bound_at"),
            "bound_by": raw.get("bound_by"),
            "note": raw.get("note")}


def resolve_target(role: str, cwd: str = "", side: dict | None = None) -> dict:
    """解析某身份当前唯一的投递目标。唯一才返回；歧义/无目标/未确认都如实报。"""
    m = side or desktop_bot()
    try:
        refresh_identity()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "identity_refresh_failed",
                "detail": "%s: %s" % (type(exc).__name__, exc)}
    url = m["base"] + "/api/harbor/candidates?role=" + urllib.parse.quote(role)
    if cwd:
        url += "&cwd=" + urllib.parse.quote(cwd)
    status, body = _get_soft(url, m["token"])
    body = body or {}
    if status == 404 and _plugin_absent(body):
        return {"ok": False, "http": 404, "error": "harbor_not_loaded",
                "detail": "港湾插件没加载（/api/harbor/candidates 返回 404）；"
                          "重启 DSH 让它加载后再解析目标",
                "basis": ["resolve_target"]}
    if body.get("ok"):
        sess = body.get("session") or {}
        return {"ok": True, "sessionId": str(sess.get("sessionId") or ""),
                "role": body.get("role") or role,
                "cwd": sess.get("cwd") or "",
                "candidates": body.get("candidates"),
                "basis": body.get("basis") or [],
                "http": status}
    return {"ok": False, "http": status,
            "error": body.get("error") or "unknown",
            "detail": body.get("detail") or "",
            "candidates": body.get("candidates") or [],
            "seen": body.get("seen") or [],
            "hint": body.get("hint") or ""}


def resolve_desktop_session(side: dict | None = None, explicit: str = "") -> dict:
    """桌面助手的目标会话：显式 > 一次性绑定 > 唯一合格。**没有"取最新"这条路。**"""
    m = side or desktop_bot()
    if explicit:
        got = verify_target(explicit, "desktop", m)
        got["basis"] = ["explicit_session_id"] + (got.get("basis") or [])
        if got.get("ok"):
            got["cwd"] = m.get("session_cwd") or ""
        return got
    origin = recent_inbound(my_session_id())
    if origin:
        got = verify_target(origin["from"], "desktop", m)
        if got.get("ok"):
            got["basis"] = ["inbound_origin@" + str(origin.get("at") or "?")] + (got.get("basis") or [])
            got["cwd"] = m.get("session_cwd") or ""
            got["origin"] = origin
            return got
        if got.get("error") == "identity_unavailable":
            return got
        return {"ok": False, "error": "origin_unavailable",
                "sessionId": origin["from"], "verify_error": got.get("error"),
                "detail": "这条是 %s 从 %s… 发来的，但那个窗口已经不可投（%s）；"
                          "不自动改投别的窗口"
                          % (origin.get("fromRole") or "对方",
                             str(origin["from"])[:14], got.get("error")),
                "origin": origin,
                "basis": ["inbound_origin@" + str(origin.get("at") or "?")]}
    bound = _binding_of(m)
    if bound:
        got = verify_target(bound["sessionId"], "desktop", m)
        if got.get("ok"):
            got["basis"] = (got.get("basis") or []) + ["bound_session@" + str(bound.get("bound_at") or "?")]
            got["cwd"] = m.get("session_cwd") or ""
            got["bound"] = bound
            return got
        # 绑定不可用（身份变了 / 窗口已关 / 已归档）——2026-11-23 归档闸要求：
        # 不死胡同停、也不恒 ok 短路；落回 candidates 检测新活候选。
        # Python 侧再剔一次归档（插件重启前也生效），剔后重算唯一性：
        #   唯一且 cwd 相符＝明确接替窗→智能自动切换；多/零→needs_confirm 找人确认。
        fresh = resolve_target("desktop", m.get("session_cwd") or "", m)
        arch = archived_session_ids()
        want_cwd = (m.get("session_cwd") or "").lower()
        if fresh.get("ok"):
            sid1 = str(fresh.get("sessionId") or "")
            live_cands = [] if sid1 in arch else [
                {"sessionId": sid1, "cwd": fresh.get("cwd") or ""}]
        else:
            live_cands = [c for c in (fresh.get("candidates") or [])
                          if str(c.get("sessionId") or "") not in arch]
        if len(live_cands) == 1:
            c0 = live_cands[0]
            cand_sid = str(c0.get("sessionId") or "")
            cand_cwd = str(c0.get("cwd") or "").lower()
            cwd_ok = (not want_cwd) or (not cand_cwd) or (want_cwd in cand_cwd) or (cand_cwd in want_cwd)
            if cand_sid and cand_sid != bound["sessionId"] and cwd_ok:
                return {"ok": True, "sessionId": cand_sid, "role": "desktop",
                        "source": "live-header", "live": True,
                        "cwd": m.get("session_cwd") or c0.get("cwd") or "",
                        "auto_switched_from": bound["sessionId"], "bound": bound,
                        "basis": ["auto_switch_after_binding_invalid",
                                  "bound_session@" + str(bound.get("bound_at") or "?"),
                                  "archived_excluded", "unique_live_candidate"]}
        return {"ok": False, "error": "binding_invalid",
                "role": got.get("role"),
                "sessionId": bound["sessionId"],
                "verify_error": got.get("error"),
                "needs_confirm": True,
                "candidates": live_cands,
                "seen": fresh.get("seen") or [],
                "detail": "通道.json 登记的 bound_session 已不可用：%s（%s）；"
                          "剔除归档后新活候选需人确认（唯一且 cwd 相符才自动切换）"
                          % (got.get("error"), got.get("detail") or ""),
                "basis": ["bound_session@" + str(bound.get("bound_at") or "?")]}
    got = resolve_target("desktop", m.get("session_cwd") or "", m)
    if got.get("ok"):
        got["basis"] = (got.get("basis") or []) + ["unique_candidate"]
    return got


def _feishu_bots() -> tuple[dict, str]:
    """哪些 bot 才算飞书助手。判据：workspaces.json 的 agentPresets 命中飞书助手预设名单。

    为什么不看"所有配了 p2p 的 bot"：一个 bot 的 state 可能是旧实例、或换了别的预设，
    拿它当证据会把"唯一"报错成唯一。范围收窄到已配置的飞书助手 bot；收不窄就报证据不完整，
    不退回"全扫一遍"。
    """
    im = os.path.join(_dsh_home(), "integrations", "dsh-feishu")
    ws = _read_json(os.path.join(im, "workspaces.json"))
    if not isinstance(ws, dict) or not isinstance(ws.get("agentPresets"), dict) \
            or not ws["agentPresets"]:
        return {}, "workspaces.json 读不出 agentPresets，无法确定哪些 bot 是飞书助手"
    try:
        refresh_identity()
    except Exception:  # noqa: BLE001
        pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import session_identity  # noqa: E402

    reg = (session_identity.load_cache().get("registry") or {})
    feishu_bot = set(reg.get("feishu_bot_presets") or [])
    if not feishu_bot:
        return {}, "身份缓存里没有飞书助手预设名单（registry 缺失），无法确定哪些 bot 是飞书助手"
    scope = {}
    for bot_id, preset in ws["agentPresets"].items():
        if str(preset) in feishu_bot:
            scope[str(bot_id)] = str(preset)
    if not scope:
        return {}, "workspaces.json 里没有一个 bot 的预设属于飞书助手名单（%s）" % ", ".join(sorted(feishu_bot))
    return scope, ""


def resolve_feishu_session() -> dict:
    """飞书助手的目标会话：**已配置的飞书助手 bot** 的 p2p 绑定 → 统一复验（R1）。

    多绑定 = 歧义；飞书助手 bot 的 state 损坏 = 证据不完整（不能断言"没有绑定"）；
    无关 bot 的损坏只记进 ignored，不参与唯一性判断。
    """
    im = os.path.join(_dsh_home(), "integrations", "dsh-feishu")
    scope, scope_error = _feishu_bots()
    if scope_error:
        return {"ok": False, "error": "evidence_incomplete", "detail": scope_error}
    cfg = _read_json(os.path.join(im, "config.json")) or {}
    bots = cfg.get("bots") or []
    if not isinstance(bots, list) or not bots:
        return {"ok": False, "error": "no_bot_configured",
                "detail": "config.json 里没有 bot，无法确认飞书助手的会话"}
    configured = [b for b in bots if str((b or {}).get("id") or "").strip() in scope]
    others = [b for b in bots if str((b or {}).get("id") or "").strip() not in scope]
    if not configured:
        return {"ok": False, "error": "no_bot_configured",
                "detail": "config.json 里的 bot 没有一个在飞书助手预设登记里",
                "scope": scope}
    missing = [bid for bid in scope if bid not in
               {str((b or {}).get("id") or "").strip() for b in configured}]
    if missing:
        return {"ok": False, "error": "evidence_incomplete",
                "detail": "workspaces.json 登记为飞书助手的 bot 不在 config.json（%s），"
                          "无法确认它是哪条会话" % ", ".join(missing)}

    found, problems, ignored = [], [], []
    for bot in configured:
        bot_id = str((bot or {}).get("id") or "").strip()
        state = _read_json(os.path.join(im, "bots", bot_id, "state.json"))
        if not isinstance(state, dict):
            problems.append("%s: state.json 读不出" % bot_id)
            continue
        sessions = state.get("sessions")
        if not isinstance(sessions, dict):
            problems.append("%s: state 里没有 sessions" % bot_id)
            continue
        for key, sid in sessions.items():
            if not str(key).startswith("p2p:"):
                continue
            sid = str(sid or "").strip()
            if sid:
                found.append({"bot_id": bot_id, "bot_name": (bot or {}).get("botName"),
                              "open_id": key[4:], "sessionId": sid})
    for bot in others:
        bot_id = str((bot or {}).get("id") or "").strip()
        if not bot_id:
            continue
        state = _read_json(os.path.join(im, "bots", bot_id, "state.json"))
        if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
            ignored.append("%s: 非飞书助手 bot，state 不可读（不影响判定）" % bot_id)

    uniq = {f["sessionId"] for f in found}
    if not uniq:
        if problems:
            return {"ok": False, "error": "evidence_incomplete", "problems": problems,
                    "ignored": ignored,
                    "detail": "飞书助手 bot 的 state 读不出来，不能断言'没有绑定'"}
        return {"ok": False, "error": "no_binding", "problems": problems,
                "ignored": ignored,
                "detail": "已配置的飞书助手 bot 里没有可用的私聊绑定"}
    if len(uniq) > 1:
        return {"ok": False, "error": "ambiguous", "candidates": found,
                "problems": problems,
                "detail": "飞书助手 bot 里有 %d 个不同的私聊会话绑定" % len(uniq)}
    origin = recent_inbound(my_session_id())
    if origin:
        got = verify_target(origin["from"], "feishu_bot")
        if got.get("ok"):
            got["basis"] = ["inbound_origin@" + str(origin.get("at") or "?")] + (got.get("basis") or [])
            got["origin"] = origin
            got["problems"], got["ignored"] = problems, ignored
            return got
        if got.get("error") != "identity_unavailable":
            return {"ok": False, "error": "origin_unavailable",
                    "sessionId": origin["from"], "verify_error": got.get("error"),
                    "detail": "这条是 %s 的窗口 %s… 发来的，但它已经不可投（%s）；不自动改投"
                              % (origin.get("fromRole") or "对方",
                                 str(origin["from"])[:14], got.get("error")),
                    "origin": origin}

    sid = found[0]["sessionId"]
    got = verify_target(sid, "feishu_bot")
    if not got.get("ok"):
        got.update({"bot": found[0], "problems": problems, "ignored": ignored,
                    "detail": "bot state 绑定的会话没通过复验：" + str(got.get("detail") or "")})
        return got
    return {"ok": True, "sessionId": sid, "role": "feishu_bot",
            "source": got.get("source"), "evidence": got.get("evidence"),
            "cache_built_at": got.get("cache_built_at"),
            "basis": ["bot_state_p2p", "identity=feishu_bot", "verify_target"],
            "bot": found[0], "problems": problems, "ignored": ignored}


def _dsh_home() -> str:
    env = os.environ.get("DSH_HOME")
    if env and os.path.isdir(env):
        return env
    up = os.environ.get("USERPROFILE")
    if up:
        return os.path.join(up, ".dsh")
    hd, hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if hd and hp:
        return os.path.join(hd + hp, ".dsh")
    return os.path.join(os.path.expanduser("~"), ".dsh")


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def archived_session_ids() -> set:
    # 已归档会话清单（2026-11-23：绝不往已归档对话投）。
    # 读 dsh_home()/storages/workspace.json 的 global.archivedSessionIds。
    # 测试可用 DSH_HOME 指到合成目录（合成 workspace.json），不污染真实清单。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import session_identity  # noqa: E402
    data = _read_json(os.path.join(session_identity.dsh_home(), "storages", "workspace.json")) or {}
    a = ((data.get("global") or {}).get("archivedSessionIds")) or []
    if isinstance(a, dict):
        a = list(a.keys())
    return {str(x) for x in a if x}


def describe(*, live: bool = False) -> int:
    """先检查本机配置。连接真实客户端需要显式选择 --live。"""
    print("正本：", os.environ.get("DSH_CHANNEL_FILE", CHANNEL))
    try:
        cfg = load()
    except (OSError, ValueError, AttributeError) as exc:
        print(f"无法读取通道配置：{exc}")
        return 2
    ok = True
    for name in ("desktop_bot", "feishu_bot"):
        side = cfg.get(name)
        if not isinstance(side, dict) or not side.get("host") or not side.get("port"):
            print(f"  ✗ {name} 缺少 host 或 port")
            ok = False
            continue
        credentials = "已配置" if side.get("token") else "待填写库外凭据"
        print(f"  ✓ {name} {side['host']}:{side['port']}｜口令{credentials}｜policy={side.get('policy', 'open')}")
    if not ok:
        return 2
    if not live:
        print("通道配置可读。接好客户端后可加 --live 检查实际会话。")
        return 0

    print("\n=== 会变的参数：按身份现场解析（不是写死的，也不是取最新）===")
    r = resolve_desktop_session()
    if r.get("ok"):
        print("  桌面助手目标会话：%s…（依据 %s）"
              % (r["sessionId"][:20], ", ".join(r.get("basis") or [])))
    else:
        print("  ✗ 桌面助手目标解析：%s%s"
              % (r.get("error"), ("｜" + str(r.get("detail"))) if r.get("detail") else ""))
        if r.get("candidates"):
            for c in r["candidates"]:
                print("      候选 %s（创建 %s）" % (c.get("sessionId"), c.get("createdAt")))
        ok = False
    q = resolve_feishu_session()
    if q.get("ok"):
        print("  飞书助手目标会话：%s…（依据 %s）"
              % (q["sessionId"][:20], ", ".join(q.get("basis") or [])))
    else:
        print("  ✗ 飞书助手目标解析：%s%s"
              % (q.get("error"), ("｜" + str(q.get("detail"))) if q.get("detail") else ""))
        ok = False
    print("\n结论：", "参数可取、两侧目标都能定位" if ok else "有问题（见上）")
    return 0 if ok else 2


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="检查通道配置")
    parser.add_argument("--live", action="store_true", help="解析两侧实际会话，需要客户端与凭据")
    args = parser.parse_args(argv)
    return describe(live=args.live)


if __name__ == "__main__":
    raise SystemExit(main())
