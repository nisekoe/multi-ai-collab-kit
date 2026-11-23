# -*- coding: utf-8 -*-
r"""会话身份判定：一条会话属于哪一面（飞书助手面 / 桌面面 / 委派 / 未知）。

为什么要有这个文件（2026-11-17 首批修复，A01/A09）
    /api/harbor/latest 只按 cwd 和活跃时间挑会话，分不清飞书助手与桌面助手：
    10:47 它返回飞书助手会话、11:2x 它返回桌面助手会话，同一条命令两个结果。
    入口与 AGENTS 又把这个接口当成"叫她"的通道，于是"她刚说完话、我刚好空闲"
    这个最常见的时刻，她会把话投给自己。

    判定身份不能靠"文件夹名字以 dfc 开头"（那只覆盖旧飞书通道），
    也不能靠"不在飞书助手映射里就是桌面助手"（旧飞书助手会话会漏出去）。
    真实可用的证据在会话首行与预设事件里：agentPreset / origin / parentSession /
    delegationDepth，加上插件维护的 bot state 与 workspaces.json 的预设登记。

它做什么
    1. 读 bot state 与 workspaces.json，知道"哪些预设属于飞书助手"；
    2. 读 profile 里注册过的预设（内置 standard/minimal/ptc/cordis、flash 两张卡、
       以及 legacy-preset-map.json 的别名），知道"哪些是已知的桌面预设"；
    3. 扫会话目录，按首行 + 预设切换事件判 role，带证据；
    4. 增量缓存到 工作台/_tmp_/会话身份.json（可重建，不是正本）。

它不做什么
    不管"会话是不是活着"——live 只有宿主插件看得见，这里不猜。
    不判定"当前我正在用哪个窗口"——那需要宿主给上下文，猜出来的都是错。

退出码 / 用法见文件尾。
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROLE_FEISHU_BOT = "feishu_bot"      # 飞书助手面（飞书那个门）
ROLE_DESKTOP = "desktop"        # 桌面/工程面（可能是桌面助手）
ROLE_DELEGATED = "delegated"    # 子代理 / 委派会话，永不作投递目标
ROLE_UNKNOWN = "unknown"        # 证据不足，不投递

CACHE_VERSION = 1


# ---------------------------------------------------------------- 路径（不靠 ~）

def dsh_home() -> str:
    """定位 .dsh。**不从环境变量裸拼**：裁剪环境下 expanduser 会返回字面量 ~。

    顺序：DSH_HOME → USERPROFILE → HOMEDRIVE+HOMEPATH → expanduser（最后兜底）。
    """
    env = os.environ.get("DSH_HOME")
    if env and os.path.isdir(env):
        return env
    up = os.environ.get("USERPROFILE")
    if up:
        p = os.path.join(up, ".dsh")
        if os.path.isdir(p):
            return p
    hd, hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if hd and hp:
        p = os.path.join(hd + hp, ".dsh")
        if os.path.isdir(p):
            return p
    return os.path.join(os.path.expanduser("~"), ".dsh")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archive_paths as ap
PROJECT = os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 缓存位置可由 DSH_IDENTITY_CACHE 覆盖——离线测试要指到临时目录，
# 否则合成数据会污染真实缓存（说明 §6：测试不向实际用户会话注入）。
CACHE_PATH = os.environ.get("DSH_IDENTITY_CACHE") or (os.path.join(PROJECT, "档案库", "工作台", "_tmp_", "会话身份.json") if ap.write_mode() == "old" else os.path.join(str(ap.paths.resolve_root(for_write=False)[0]), "_local", "cache", "会话身份.json"))
IM_DIR = os.path.join(dsh_home(), "integrations", "dsh-feishu")
PROFILE_DIR = os.path.join(dsh_home(), "profiles", "desktop")


def sessions_root() -> str:
    """会话根目录；测试可以用 DSH_TEST_SESSIONS 指到合成目录（绝不写真实会话）。"""
    env = os.environ.get("DSH_TEST_SESSIONS")
    if env:
        return env
    return os.path.join(dsh_home(), "sessions")


# ---------------------------------------------------------------- 注册表

# 上游常驻 roster（legacy-preset-map.json 的 _note 明确写了这四个）
BUILTIN_PRESETS = {"standard", "minimal", "ptc", "cordis"}
FLASH_PRESETS_DIR = os.path.join(PROFILE_DIR, "node_modules", "dsh-flash-presets", "presets")


def _read_json(path: str):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def load_registry() -> dict:
    """收集：飞书助手预设集合、已知桌面预设集合，以及每类证据的来源路径。"""
    evidence = {}

    # 1) 飞书助手：workspaces.json 的 agentPresets 值（插件自己登记，比写死可靠）
    feishu_bot = set()
    ws = _read_json(os.path.join(IM_DIR, "workspaces.json"))
    if isinstance(ws, dict):
        for v in (ws.get("agentPresets") or {}).values():
            if isinstance(v, str) and v:
                feishu_bot.add(v)
        evidence["feishu_bot_presets"] = os.path.join(IM_DIR, "workspaces.json")
    # 她的 preset 源码目录在 workspace 里，名字就是 feishu_bot
    if os.path.isdir(os.path.join(PROJECT, "..", "deepseek-harness")):
        pass
    if not feishu_bot:
        feishu_bot.add("feishu_bot")

    # 2) 桌面：内置 roster + flash 两张卡 + 别名表
    desk = set(BUILTIN_PRESETS)
    evidence["builtin_presets"] = "legacy-preset-map.json 的 _note（上游常驻 roster）"
    try:
        for fn in sorted(os.listdir(FLASH_PRESETS_DIR)):
            if not fn.endswith(".patch.yml"):
                continue
            try:
                with io.open(os.path.join(FLASH_PRESETS_DIR, fn), encoding="utf-8") as fh:
                    for line in fh:
                        s = line.strip()
                        if s.startswith("id:"):
                            desk.add(s[3:].strip())
                            break
            except OSError:
                continue
        evidence["flash_presets"] = FLASH_PRESETS_DIR
    except OSError:
        pass
    lmap = _read_json(os.path.join(FLASH_PRESETS_DIR, "legacy-preset-map.json"))
    if isinstance(lmap, dict):
        for k in (lmap.get("aliases") or {}):
            desk.add(k)
        evidence["legacy_aliases"] = os.path.join(FLASH_PRESETS_DIR, "legacy-preset-map.json")

    # 一个 feishu bot 被配成桌面 roster 里的预设（例如误填 standard）时，
    # **不能**把 standard 也算成飞书助手预设——那会让所有桌面会话失去判据。
    # 记录冲突、把它从飞书助手集合里摘掉，让桌面判据保持完整。
    conflict = sorted(feishu_bot & desk)
    if conflict:
        evidence["ignored_conflicts"] = conflict
        feishu_bot -= desk
    desk -= feishu_bot
    return {
        "feishu_bot_presets": sorted(feishu_bot),
        "desktop_presets": sorted(desk),
        "legacy_feishu_prefix": os.environ.get("FEISHU_LEGACY_SESSION_PREFIX", "dfc:"),
        "evidence": evidence,
    }


# ---------------------------------------------------------------- 读会话文件

def _decompress(path: str) -> bytes:
    import zstandard  # 只有 astrbot 那个解释器带它

    data = open(path, "rb").read()
    d = zstandard.ZstdDecompressor()
    try:
        return d.decompress(data)
    except Exception:  # noqa: BLE001
        chunks = []
        with d.stream_reader(io.BytesIO(data)) as reader:
            while True:
                chunk = reader.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)


def read_header_and_presets(path: str) -> tuple[dict, list[dict]]:
    """返回（首行 header, 预设切换事件列表）。只解析需要的那几行。"""
    raw = _decompress(path)
    text = raw.decode("utf-8", "replace")
    header = {}
    presets: list[dict] = []
    first = True
    for line in text.splitlines():
        if not line.strip():
            continue
        if first:
            first = False
            try:
                header = json.loads(line)
            except json.JSONDecodeError:
                header = {}
            continue
        if '"agent-preset/selected"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "agent-preset/selected":
            continue
        data = ev.get("data") or {}
        presets.append({
            "seq": ev.get("seq"),
            "time": ev.get("time"),
            "agentPreset": data.get("agentPreset"),
        })
    return header, presets


# ---------------------------------------------------------------- 判定

def classify(header: dict, presets: list[dict], registry: dict) -> dict:
    """按证据判身份。返回 role + 证据链 + 用到的预设。"""
    sid = str(header.get("id") or "")
    creation = header.get("agentPreset")
    depth = header.get("delegationDepth")
    origin = header.get("origin")
    parent = header.get("parentSession")

    seen = []
    if isinstance(creation, str) and creation:
        seen.append(creation)
    for p in presets:
        v = p.get("agentPreset")
        if isinstance(v, str) and v and v not in seen:
            seen.append(v)
    effective = presets[-1].get("agentPreset") if presets else creation

    feishu_bot = set(registry.get("feishu_bot_presets") or [])
    desk = set(registry.get("desktop_presets") or [])
    legacy_prefix = str(registry.get("legacy_feishu_prefix") or os.environ.get("FEISHU_LEGACY_SESSION_PREFIX", "dfc:"))
    ev: list[str] = []

    # 1) 委派会话：三种标记任一命中。**必须先判**——旧通道前缀也能派子任务，
    #    委派出来的会话永不作投递目标，先认前缀会把它当成飞书助手面。
    if origin == "subagent" or (isinstance(depth, int) and depth > 0) or parent:
        ev.append("origin=%r delegationDepth=%r parentSession=%r" % (origin, depth, parent))
        return {"role": ROLE_DELEGATED, "effective_preset": effective,
                "presets_seen": seen, "evidence": ev, "reason": "委派会话"}

    # 2) 旧飞书通道：id 前缀就是通道编码（可由 FEISHU_LEGACY_SESSION_PREFIX 覆盖）
    if legacy_prefix and sid.startswith(legacy_prefix):
        ev.append("session_id 前缀 %s（旧飞书通道）" % legacy_prefix)
        return {"role": ROLE_FEISHU_BOT, "effective_preset": effective,
                "presets_seen": seen, "evidence": ev, "reason": "旧通道前缀"}

    # 3) 出现过的预设里有飞书助手的 → 飞书助手面（历史会话也算，不要求等于当前绑定）
    hit = [p for p in seen if p in feishu_bot]
    if hit:
        ev.append("预设命中飞书助手登记：%s" % ", ".join(hit))
        return {"role": ROLE_FEISHU_BOT, "effective_preset": effective,
                "presets_seen": seen, "evidence": ev, "reason": "飞书助手预设"}

    # 4) 顶层 + 预设全部已知且非飞书助手 → 桌面面
    if seen and all(p in desk for p in seen):
        ev.append("顶层会话；预设 %s 均在已知桌面 roster" % ", ".join(seen))
        return {"role": ROLE_DESKTOP, "effective_preset": effective,
                "presets_seen": seen, "evidence": ev, "reason": "已知桌面预设"}

    # 5) 其余一律未知（含预设缺失、未登记的新预设、证据冲突）
    if not seen:
        ev.append("首行与事件里都没有 agentPreset")
    else:
        ev.append("预设不在已知 roster：%s" % ", ".join(seen))
    return {"role": ROLE_UNKNOWN, "effective_preset": effective,
            "presets_seen": seen, "evidence": ev, "reason": "证据不足"}


def classify_segment(header: dict, preset: str | None, registry: dict) -> str:
    """按"某一段时间里生效的预设"判身份（归档用；预设中途换过就按段说）。

    与 classify() 的差别：classify 看**整条会话出现过的所有预设**（路由要保守，
    飞书助手换过预设就整条算飞书助手面）；而归档要能说"这段时间是 desktop、那段时间是 feishu_bot"，
    所以这里只看给定那一刻的预设。
    """
    sid = str(header.get("id") or "")
    depth = header.get("delegationDepth")
    origin = header.get("origin")
    parent = header.get("parentSession")
    # 委派优先：旧通道前缀会话派出的子任务仍算委派，不算飞书助手面。
    if origin == "subagent" or (isinstance(depth, int) and depth > 0) or parent:
        return ROLE_DELEGATED
    legacy_prefix = str(registry.get("legacy_feishu_prefix") or os.environ.get("FEISHU_LEGACY_SESSION_PREFIX", "dfc:"))
    if legacy_prefix and sid.startswith(legacy_prefix):
        return ROLE_FEISHU_BOT
    if isinstance(preset, str) and preset:
        if preset in set(registry.get("feishu_bot_presets") or []):
            return ROLE_FEISHU_BOT
        if preset in set(registry.get("desktop_presets") or []):
            return ROLE_DESKTOP
    return ROLE_UNKNOWN


# ---------------------------------------------------------------- 缓存

def load_cache() -> dict:
    c = _read_json(CACHE_PATH)
    if not isinstance(c, dict) or c.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "sessions": {}}
    c.setdefault("sessions", {})
    return c


def refresh(force: bool = False) -> dict:
    """增量扫描并落缓存。返回缓存字典（含 built_at / registry / sessions）。"""
    root = sessions_root()
    registry = load_registry()
    cache = {"version": CACHE_VERSION, "sessions": {}} if force else load_cache()
    old = cache.get("sessions") or {}
    new: dict[str, dict] = {}
    parsed = 0
    reused = 0
    errors: list[str] = []

    try:
        cwd_dirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError as exc:
        return {"version": CACHE_VERSION, "sessions": {}, "built_at": _now(),
                "root": root, "registry": registry, "error": "读不到会话根：%s" % exc}

    for cwd_dir in cwd_dirs:
        base = os.path.join(root, cwd_dir)
        try:
            sess_dirs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        except OSError:
            continue
        for sess in sess_dirs:
            f = os.path.join(base, sess, "session.v4.jsonl.zstd")
            if not os.path.isfile(f):
                continue
            try:
                st = os.stat(f)
            except OSError:
                continue
            stamp = [st.st_size, st.st_mtime_ns]
            prev = old.get(sess)
            if (not force and isinstance(prev, dict)
                    and prev.get("_stamp") == stamp
                    and prev.get("_registry_id") == _registry_id(registry)):
                new[sess] = prev
                reused += 1
                continue
            try:
                header, presets = read_header_and_presets(f)
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: %s" % (sess[:20], type(exc).__name__))
                continue
            cls = classify(header, presets, registry)
            row = {
                "session_id": str(header.get("id") or sess),
                "dir_name": sess,
                "cwd": header.get("cwd"),
                "created_at": header.get("createdAt"),
                "creation_preset": header.get("agentPreset"),
                "effective_preset": cls["effective_preset"],
                "presets_seen": cls["presets_seen"],
                "preset_history": presets,
                "origin": header.get("origin"),
                "delegation_depth": header.get("delegationDepth"),
                "parent_session": header.get("parentSession"),
                "role": cls["role"],
                "reason": cls["reason"],
                "evidence": cls["evidence"],
                "file": f,
                "_stamp": stamp,
                "_registry_id": _registry_id(registry),
            }
            new[sess] = row
            parsed += 1

    cache["sessions"] = new
    cache["root"] = root
    cache["registry"] = registry
    cache["built_at"] = _now()
    cache["stats"] = {"parsed": parsed, "reused": reused, "total": len(new),
                      "errors": errors[:20], "error_count": len(errors)}
    _write_cache(cache)
    return cache


def _registry_id(registry: dict) -> str:
    import hashlib

    raw = json.dumps([registry.get("feishu_bot_presets"), registry.get("desktop_presets")],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_cache(cache: dict) -> None:
    sys.path.insert(0, os.path.join(PROJECT, "tools"))
    try:
        from safe_write import write_text
    except Exception:  # noqa: BLE001
        write_text = None
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    text = json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if write_text:
        write_text(CACHE_PATH, text)
    else:
        with io.open(CACHE_PATH, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


# ---------------------------------------------------------------- 查询

def lookup(sid: str, cache: dict | None = None) -> dict | None:
    cache = cache or load_cache()
    for row in (cache.get("sessions") or {}).values():
        if row.get("session_id") == sid or row.get("dir_name") == sid:
            return row
    return None


def candidates(cwd_keyword: str, role: str, cache: dict | None = None) -> list[dict]:
    """按 cwd 关键词 + role 过滤。**不按时间排，也不替调用方选。**"""
    cache = cache or load_cache()
    out = []
    for row in (cache.get("sessions") or {}).values():
        if row.get("role") != role:
            continue
        if cwd_keyword and cwd_keyword.lower() not in str(row.get("cwd") or "").lower():
            continue
        out.append(row)
    return sorted(out, key=lambda r: -(r.get("created_at") or 0))


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="会话身份判定与缓存（首批修复 A01/A09）")
    ap.add_argument("--refresh", action="store_true", help="重扫（增量）并落缓存")
    ap.add_argument("--force", action="store_true", help="忽略缓存戳，全量重扫")
    ap.add_argument("--who", help="查某条会话的身份")
    ap.add_argument("--candidates", help="按 cwd 关键词列合格目标")
    ap.add_argument("--role", default=ROLE_DESKTOP,
                    choices=[ROLE_FEISHU_BOT, ROLE_DESKTOP, ROLE_DELEGATED, ROLE_UNKNOWN])
    ap.add_argument("--json", action="store_true", help="机器可读")
    args = ap.parse_args(argv)

    if args.refresh or args.force:
        cache = refresh(force=args.force)
    else:
        cache = load_cache()
    if not cache.get("sessions"):
        cache = refresh()

    if args.who:
        row = lookup(args.who, cache)
        if not row:
            print("没有这条会话：%s" % args.who)
            return 1
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=1))
        else:
            print("会话 %s" % row["session_id"])
            print("  身份 %s（%s）" % (row["role"], row["reason"]))
            print("  预设 %s -> 现在 %s" % (row["creation_preset"], row["effective_preset"]))
            for e in row["evidence"]:
                print("  证据 %s" % e)
        return 0

    if args.candidates is not None:
        rows = candidates(args.candidates, args.role, cache)
        if args.json:
            print(json.dumps({"role": args.role, "count": len(rows),
                              "items": [{"sessionId": r["session_id"],
                                         "createdAt": r.get("created_at"),
                                         "basis": r["reason"]} for r in rows]},
                             ensure_ascii=False, indent=1))
        else:
            print("身份 %s 的候选 %d 个：" % (args.role, len(rows)))
            for r in rows:
                print("  %s  %s  (%s)" % (r["session_id"], r["reason"], r["effective_preset"]))
        return 0 if rows else 3

    st = cache.get("stats") or {}
    print("缓存：%s" % CACHE_PATH)
    print("  构建 %s ｜ 会话 %s 条（解析 %s / 复用 %s / 出错 %s）"
          % (cache.get("built_at"), st.get("total"), st.get("parsed"),
             st.get("reused"), st.get("error_count")))
    from collections import Counter

    c = Counter(r["role"] for r in (cache.get("sessions") or {}).values())
    for k in (ROLE_FEISHU_BOT, ROLE_DESKTOP, ROLE_DELEGATED, ROLE_UNKNOWN):
        print("  %-10s %d" % (k, c.get(k, 0)))
    reg = cache.get("registry") or {}
    print("  飞书助手预设：%s" % ", ".join(reg.get("feishu_bot_presets") or []))
    print("  桌面预设：%s" % ", ".join(reg.get("desktop_presets") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
