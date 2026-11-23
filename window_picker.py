# -*- coding: utf-8 -*-
"""window_picker.py — Unified window picker across three legs (harbor / codex / Porta).

Design: 内部设计文档（略） (three-party: review_bot Porta / GPT codex / desktop_bot harbor+arch).
Core invariant: NEVER guess. Unique qualified target -> deliver; multiple -> human pick
(keyword / scrollable card); zero -> no_target. All entries reuse verify_target.

Leg-agnostic core: UnifiedCandidate model, unified exit codes, verify_target,
deterministic keyword matching, select orchestrator. Per-leg adapters plug in via
the Adapter protocol; concrete impls integrated from GPT (codex) / review_bot (Porta) drafts.

License: MIT (mechanism layer). Status: foundation v0.1, adapters pending integration.
"""
from __future__ import annotations

import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Protocol

EXIT_OK = 0
EXIT_ENV = 2
EXIT_NO_TARGET = 3
EXIT_AMBIGUOUS = 4
EXIT_VERIFY_FAIL = 5
EXIT_UNQUALIFIED = 6
EXIT_BUSY = 7
EXIT_CANCELLED = 10


@dataclass
class UnifiedCandidate:
    leg: str
    hostId: str
    id: str
    title: str = ""
    alias: str = ""
    display_title: str = ""
    role: str = "unknown"
    cwd: str = ""
    status: str = "unknown"
    lastActivity: str = ""
    busySecondsAgo: int | None = None
    reachable: bool = True
    unusableReason: str = ""
    truncated: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def key(self):
        return (self.leg, self.hostId, self.id)


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return " ".join(s.split()).casefold()


def decode_uri(s: str) -> str:
    if not s:
        return ""
    return urllib.parse.unquote(str(s))


def verify_target(c: UnifiedCandidate, allow_protected: bool = False) -> tuple[bool, str]:
    reason = c.unusableReason
    if reason == "archived" or c.status == "archived":
        return False, "archived"
    if reason in ("cloud_no_rollout", "unloaded_disk_only", "no_local_record"):
        return False, reason
    if reason == "workspace_protected":
        return (True, "ok") if allow_protected else (False, "workspace_protected")
    if not c.reachable:
        return False, reason or "unreachable"
    return True, "ok"


def keyword_match(candidates: list[UnifiedCandidate], kw: str) -> list[UnifiedCandidate]:
    kw_n = _norm(kw)
    if not kw_n:
        return []
    hits, seen = [], set()
    for c in candidates:
        if not verify_target(c)[0]:
            continue
        fields = [c.alias, c.title, c.display_title, decode_uri(c.cwd),
                  c.meta.get("projectName", ""), c.id[:8]]
        if any(kw_n in _norm(f) for f in fields if f):
            if c.key not in seen:
                seen.add(c.key)
                hits.append(c)
    return hits


def _dedup(cands: list[UnifiedCandidate]) -> list[UnifiedCandidate]:
    seen, out = set(), []
    for c in cands:
        if c.key not in seen:
            seen.add(c.key)
            out.append(c)
    return out


def select(candidates: list[UnifiedCandidate], explicit: str = "", keyword: str = "",
           allow_protected: bool = False) -> tuple[int, object]:
    cands = _dedup(candidates)
    if any(c.truncated for c in cands):
        if not explicit and not keyword:
            return EXIT_AMBIGUOUS, [c for c in cands if verify_target(c, allow_protected)[0]]
    if explicit:
        ex_n = _norm(explicit)
        hits = _dedup([c for c in cands if c.id == explicit or _norm(c.alias) == ex_n
                       or _norm(c.display_title) == ex_n or _norm(c.title) == ex_n])
        if not hits:
            return EXIT_NO_TARGET, "无效地址：%s（查无此 id/别名）" % explicit
        if len(hits) > 1:
            return EXIT_AMBIGUOUS, hits
        ok, reason = verify_target(hits[0], allow_protected)
        return (EXIT_OK, hits[0]) if ok else (EXIT_UNQUALIFIED, (hits[0], reason))
    if keyword:
        hits = keyword_match(cands, keyword)
        if not hits:
            return EXIT_NO_TARGET, "关键字无命中：%s（改词或出全表）" % keyword
        if len(hits) > 1:
            return EXIT_AMBIGUOUS, hits
        ok, reason = verify_target(hits[0], allow_protected)
        return (EXIT_OK, hits[0]) if ok else (EXIT_UNQUALIFIED, (hits[0], reason))
    qualified = [c for c in cands if verify_target(c, allow_protected)[0]]
    if not qualified:
        return EXIT_NO_TARGET, "无合格目标"
    if len(qualified) > 1:
        return EXIT_AMBIGUOUS, qualified
    return EXIT_OK, qualified[0]


class Adapter(Protocol):
    leg: str
    def enumerate(self) -> list[UnifiedCandidate]: ...
    def deliver(self, candidate: UnifiedCandidate, msg: str) -> dict: ...


def prefilter_active(candidates: list[UnifiedCandidate]) -> list[UnifiedCandidate]:
    return [c for c in candidates if verify_target(c)[0]]

# ---- Selection card formatting (User UX: title-first, paginated full active list) ----
import math
from collections import Counter

PAGE_SIZE = 8
NAV_NEXT = "下一页 →"
NAV_KEYWORD = "输入关键字缩小"
NAV_CANCEL = "取消／不投"
GROUP_HDR_SESSION = "〔会话区〕"


def group_of(c: UnifiedCandidate) -> tuple:
    """镜像平台原生分组：有 projectName 归项目区（如 Porta「demo-alpha」），否则独立会话区。
    codex/harbor 原生分组待 peer 核实，暂统一会话区（不臆测）。"""
    proj = (c.meta.get("projectName") or "").strip()
    return ("项目", proj) if proj else ("会话", "")


def _cwd_short(cwd: str) -> str:
    p = decode_uri(cwd).rstrip("/\\")
    return p.replace("\\", "/").split("/")[-1] if p else ""


def _status_tag(c: UnifiedCandidate) -> str:
    return {"idle": "空闲", "running": "正忙", "busy_recent": "刚输入",
            "persisted_cold": "冷会话", "unknown": ""}.get(c.status, c.status)


def render_rows(cands: list[UnifiedCandidate]) -> list[dict]:
    """Active set only (archived pre-excluded). Title-first; unique labels;
    duplicate titles auto-get a short-id suffix + extra subtitle (UX §12/§5.3)."""
    active = prefilter_active(cands)
    counts = Counter((c.title or c.alias or c.id[:8]) for c in active)
    rows, used = [], set()
    for c in active:
        base = c.title or c.alias or c.id[:8]
        dup = counts[base] > 1
        label = base
        if dup:
            label = "%s (%s)" % (base, c.id[:4])
        k = 4
        while label in used:  # guarantee unique label even on short-id collision
            k += 2
            label = "%s (%s)" % (base, c.id[:k])
        used.add(label)
        desc = [ "%s·%s" % (c.leg, c.role), _status_tag(c), _cwd_short(c.cwd) ]
        if dup:
            desc += [c.lastActivity, c.id[:4]]
        rows.append({"label": label,
                     "description": " | ".join(p for p in desc if p),
                     "key": c.key, "candidate": c})
    return rows


def build_card(cands: list[UnifiedCandidate], page: int = 0, page_size: int = PAGE_SIZE) -> dict:
    """Prepare ask_user_question args: paginated active list + next/keyword/cancel.

    Works whether or not the UI supports true scrolling (pagination is the safe
    fallback, v1.0 §5.4 path B). No default submit; cancel always present.
    """
    rows = render_rows(cands)
    total = len(rows)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    page_rows = rows[page * page_size:(page + 1) * page_size]
    options = [{"label": r["label"], "description": r["description"]} for r in page_rows]
    if page < pages - 1:
        options.append({"label": NAV_NEXT, "description": "第%d/%d页" % (page + 2, pages)})
    options.append({"label": NAV_KEYWORD, "description": "念几个独特字"})
    options.append({"label": NAV_CANCEL, "description": "放弃本次投递"})
    question = {"id": "pick_window", "header": "选窗",
                "question": "投到哪个对话？（%d个活跃，第%d/%d页）" % (total, page + 1, pages),
                "options": options}
    return {"question": question, "rows": rows, "page": page, "pages": pages, "total": total}


def build_card_grouped(cands: list[UnifiedCandidate], scroll_cap: int = 60) -> dict:
    """路 A 真滚动分组卡（2026-11-22 用户实核 30 项流畅成立）：镜像平台侧边栏排列——
    项目区（按 projectName 分组）＋会话区（独立会话按最近活跃）。**分组名做成问题标题 header，
    不是选项**——用户反馈「分组名也是选项怕错选」，故每组一个 question，标题即分组名（不可选、一眼区分），
    组内选项才是真正能投的对话。归档/受保护已由 render_rows→prefilter_active 预排除。
    超 scroll_cap 兜底回 build_card 分页（路 B）。返回 {questions:[...], rows, grouped, total}。"""
    rows = render_rows(cands)
    total = len(rows)
    if total > scroll_cap:
        fb = build_card(cands)
        return {"questions": [fb["question"]], "rows": fb["rows"], "grouped": False,
                "total": total, "page": fb["page"], "pages": fb["pages"]}
    for r in rows:
        r["group"] = group_of(r["candidate"])
    proj_rows = [r for r in rows if r["group"][0] == "项目"]
    sess_rows = [r for r in rows if r["group"][0] == "会话"]
    questions = []
    if proj_rows:
        groups = {}
        for r in proj_rows:
            groups.setdefault(r["group"][1], []).append(r)
        for i, (gname, grp) in enumerate(groups.items()):
            questions.append({"id": "grp_proj_%d" % i, "header": "项目 · %s" % gname,
                              "question": "───── 项目 · %s ─────\n%d 个对话 · 选一个投；想不起名字直接打字筛；跳过或打「取消」＝不投" % (gname, len(grp)),
                              "options": [{"label": r["label"], "description": r["description"]} for r in grp]})
    if sess_rows:
        questions.append({"id": "grp_session", "header": "会话区",
                          "question": "───── 会话区 ─────\n独立会话（%d 个）· 按最近活跃 · 选一个投；想不起名字直接打字筛；跳过或打「取消」＝不投" % len(sess_rows),
                          "options": [{"label": r["label"], "description": r["description"]} for r in sess_rows]})
    return {"questions": questions, "rows": rows, "grouped": True, "total": total}


def resolve_selection(card: dict, selected_label: str) -> tuple[str, object]:
    """Map a card selection back to a candidate / navigation token.

    Returns ('ok', candidate) | ('cancelled', None) | ('next_page', None)
            | ('keyword', None) | ('not_found', None).
    Caller must verify_target(candidate) again before deliver (stale-window guard).
    """
    if selected_label == NAV_CANCEL:
        return ("cancelled", None)
    if selected_label == NAV_NEXT:
        return ("next_page", None)
    if selected_label == NAV_KEYWORD:
        return ("keyword", None)
    if selected_label == GROUP_HDR_SESSION or selected_label.startswith("〔项目〕"):
        return ("group_header", None)
    for r in card["rows"]:
        if r["label"] == selected_label:
            return ("ok", r["candidate"])
    return ("not_found", None)


def resolve_grouped(card: dict, answers: list) -> tuple[str, object]:
    """多问题分组卡回执映射：answers 为 ask_user_question 返回 [{id,selected,custom}]。
    分组名是 question header（不可选），故选中的必是某组内对话或 nav 操作。
    取第一个非空 selected：NAV_CANCEL→cancelled／NAV_KEYWORD→keyword／否则按 label 找候选→ok。
    全空（全跳过）→no_selection。投前仍须 verify_target 复验（stale-window guard）。"""
    if "option_map" in card:  # 按字段存在判v2（畸形option_map=None也不得绕过，GPT桥复审#6）
        # v2 分页/排序卡必须走 v2 解码器（当前页＋题ID＋实际选项验票）；
        # 旧解析器全量 rows 会放行未展示项（GPT复审#6），v1 卡无 option_map 不受影响。
        return ("v2_card_use_v2_resolver", None)
    for a in answers:
        custom = (a.get("custom") or "").strip()
        if custom:
            if custom in ("取消", "不投", "取消／不投", "cancel", "算了"):
                return ("cancelled", None)  # 打字「取消」＝不投
            return ("keyword", custom)  # 第一题直接打字＝关键字筛选（用户 2026-11-22：不用记全名）
        sel = a.get("selected") or []
        if not sel:
            continue
        label = sel[0]
        if label == NAV_CANCEL:
            return ("cancelled", None)
        if label == NAV_KEYWORD:
            return ("keyword", None)
        if label == NAV_NEXT:
            return ("next_page", None)  # v2分页卡翻页（2026-11-22 调度层合入批；v1分组卡不渲染此词，行为不变）
        for r in card["rows"]:
            if r["label"] == label:
                return ("ok", r["candidate"])
        return ("not_found", None)
    return ("cancelled", None)  # 全跳过＝不投（用户 2026-11-22：不投直接跳过）


def build_card_grouped_filtered(cands: list[UnifiedCandidate], kw: str, scroll_cap: int = 60) -> dict:
    """第一题打字关键字→筛后重渲染分组卡（用户 2026-11-22：不用记全名，直接打字筛）。
    keyword_match 已含 verify_target 预排除＋去重；无命中返回空卡（caller 提示改词或出全表）。"""
    return build_card_grouped(keyword_match(cands, kw), scroll_cap=scroll_cap)

