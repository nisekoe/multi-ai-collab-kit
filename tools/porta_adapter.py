# -*- coding: utf-8 -*-
r"""porta_adapter.py — 统一选窗器 · Porta（Antigravity）腿适配器实现草稿。

定位：
    替代裸 curl.exe 调用本机 Porta 代理（127.0.0.1:3170），实现《统一选窗器设计 v1.0》
    （§2 统一候选模型 / §3 适配器契约 / §4.3 Porta 腿 / §5 选窗交互 / §6 统一退出码 / §12 用户定调）
    的全部只读枚举、四层身份/歧义保护、卡片预排除、关键字匹配与投后真证据核验。

依据（实测＋源码只读核查，2026-11-22）：
    - Porta 代理源码：tools\porta\packages\proxy\src\index.ts（/api/health）
      routes/conversations.ts（/api/conversations 上限 MAX_TOTAL_CONVERSATIONS=100、
      _diskOnly/UNLOADED、/steps?tail=、POST /messages 无 RUNNING 拦截）
      message-tracker.ts（clientMessageId 标注于 step 顶层）
    - 本机只读实测：/api/conversations 返回 14 字段，顶层无 title 键，标题在 annotations.title / summary，
      workspaces[].workspaceFolderAbsoluteUri 为 URL 编码 file:/// URI，annotations.archived 标归档，
      convId（dict key / rootConversationId）与 trajectoryId 是不同 UUID。

写边界：
    本文件为评审助手 lab 侧草稿模块（不直接修改 项目根\tools\），
    供桌面助手复审后集成进统一选窗器；默认内置只读安全与 dry_run 支持。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import msg_envelope
import receipt_tri

# ── 统一退出码（对齐 v1.0 §6） ──────────────────────────────────────────────
EXIT_OK = 0
EXIT_ENV = 2                  # 环境或桥异常（Porta 不可达 / 无存活 LS / 协议解析错）
EXIT_NO_TARGET = 3            # 无合格目标
EXIT_AMBIGUOUS = 4            # 歧义需点名（或列表截断禁自动放行）
EXIT_DELIVER_VERIFY_FAIL = 5  # 投后核验失败
EXIT_TARGET_INVALID = 6       # 目标不合格（归档 / 冷 / 受保护 / 误传 trajectoryId / projectId 漂移）
EXIT_BUSY = 7                 # 正忙（RUNNING 或 60s 内刚有用户输入）
EXIT_USER_CANCELLED = 10      # 用户在选窗卡中点取消

# ── Porta 协议常量（对齐 conversations.ts） ────────────────────────────────
DEFAULT_PORTA_BASE = os.environ.get("PORTA_BASE", "http://127.0.0.1:3170")
PORTA_MAX_CONVERSATIONS = 100
DEFAULT_BUSY_WINDOW_SEC = 60
DEFAULT_POLL_TIMEOUT_SEC = 60
DEFAULT_POLL_INTERVAL_SEC = 2.0

STATUS_IDLE = "CASCADE_RUN_STATUS_IDLE"
STATUS_RUNNING = "CASCADE_RUN_STATUS_RUNNING"
STATUS_UNLOADED = "CASCADE_RUN_STATUS_UNLOADED"

STEP_USER_INPUT = "CORTEX_STEP_TYPE_USER_INPUT"
STEP_PLANNER_RESPONSE = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
STEP_STATUS_DONE = "CORTEX_STEP_STATUS_DONE"

# 与 tools/msg_envelope.py 保持一致的 ORIGIN / 信封正则
ORIGIN_PAT = re.compile(r"^\[ORIGIN\s+peer_agent=(?P<name>[^\s\]|]+)[^\]]*\]\s*$")
ENVELOPE_PAT = re.compile(r"^〔[^〕]*〕\s*$")

# 默认受保护工作区模式（可通过 env PROTECTED_WORKSPACES 或配置覆盖/扩展）
_ENV_PROTECTED_PORTA = tuple(
    p.strip() for p in os.environ.get("PROTECTED_WORKSPACES", "").split(",") if p.strip()
)
DEFAULT_PROTECTED_PATTERNS: Tuple[str, ...] = _ENV_PROTECTED_PORTA or (
    "protected-workspace",
    "private-zone",
)


# ── 工具函数 ────────────────────────────────────────────────────────────────

def _utc_now_ts() -> float:
    return time.time()


def _iso_stamp(now_ts: Optional[float] = None) -> str:
    t = now_ts if now_ts is not None else time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def parse_iso_utc(value: Any) -> Optional[float]:
    """解析 Porta 返回的 ISO-8601 时间戳（含纳秒精度如 .663404300Z）为 Unix 秒。"""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Python 3.12 fromisoformat 仅支持到微秒（6 位小数），超长小数截断至 6 位
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$", s)
    if not m:
        return None
    base, frac, tz = m.group(1), m.group(2) or "", m.group(3) or "Z"
    if frac:
        frac = (frac[:6]).ljust(6, "0")
        iso = f"{base}.{frac}{'+00:00' if tz == 'Z' else tz}"
    else:
        iso = f"{base}{'+00:00' if tz == 'Z' else tz}"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def decode_file_uri(uri: str) -> str:
    """将 Porta 的 file:/// URI（含中文百分号编码）解码为规范本地路径。"""
    if not uri or not isinstance(uri, str):
        return ""
    raw = uri.strip()
    if raw.lower().startswith("file:///"):
        raw = raw[8:]
    elif raw.lower().startswith("file://"):
        raw = raw[7:]
    decoded = urllib.parse.unquote(raw)
    # 统一盘符大小写与斜杠（例如 x:/work/demo -> X:/work/demo）
    decoded = decoded.replace("\\", "/")
    if len(decoded) >= 2 and decoded[1] == ":":
        decoded = decoded[0].upper() + decoded[1:]
    return decoded.rstrip("/")


def normalize_text(value: str) -> str:
    """Unicode NFKC 规范化＋折叠空白＋小写，用于确定性子串关键字匹配。"""
    s = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(s.split()).lower()


def origin_line(peer_agent: str) -> str:
    return f"[ORIGIN peer_agent={peer_agent} | trust=peer_only | no_irreversible_approval]"


def wrap_origin_idempotent(text: str, peer_agent: str) -> str:
    """强制首行 ORIGIN 信任名牌，已存在则不重复（幂等）。"""
    body = (text or "").strip()
    peer = (peer_agent or "").strip()
    lines = body.split("\n")
    has_origin = bool(lines and ORIGIN_PAT.match(lines[0].strip()))
    if has_origin:
        if not peer:
            return body
        # 若调用方显式指定 peer_agent，确保首行规范一致但不叠加两行
        rest = "\n".join(lines[1:]).lstrip("\n")
        return origin_line(peer) + ("\n" + rest if rest else "")
    if not peer:
        return body
    return origin_line(peer) + ("\n" + body if body else "")


def binding_verdict(binding: str, chosen: str, rows: list) -> dict:
    """投前现查：绑定是不是已经被更新的会话取代。

    rows: [{"id": str, "last": str}]，按 last 倒序排好（新的在前）。
    - 选的是最新一条：放行；
    - 没有绑定：拒（不能自己挑）；
    - 选的是旧绑定、且它是最新：放行；
    - 选的是旧绑定、但已有更新的：拒（new_window_needs_confirm）；
    - 既不是绑定也不是最新：拒（chosen_not_bound）。
    """
    if not rows:
        return {"ok": True, "reason": "no_rows"}
    newest = rows[0]
    if chosen.startswith(newest["id"][:8]):
        return {"ok": True, "reason": "chosen_newest"}
    if not binding:
        return {"ok": False, "reason": "no_binding_need_confirm", "newer": newest}
    idx = next((i for i, r in enumerate(rows) if r["id"].startswith(binding[:8])), None)
    if idx is None:
        return {"ok": False, "reason": "binding_missing", "newer": newest}
    if not (chosen.startswith(binding[:8]) or binding.startswith(chosen[:8])):
        return {"ok": False, "reason": "chosen_not_bound", "newer": newest}
    if idx == 0:
        return {"ok": True, "reason": "bound_target_no_newer"}
    return {"ok": False, "reason": "new_window_needs_confirm", "newer": newest}


def extract_body_fingerprint(wrapped_text: str, max_len: int = 0) -> str:
    """跳过固定 ORIGIN 首行与信封〔〕头，提取真实正文指纹（修「69字头吃掉前40字」老坑）。"""
    lines = (wrapped_text or "").replace("\r\n", "\n").split("\n")
    idx = 0
    while idx < len(lines):
        s = lines[idx].strip()
        if not s or ORIGIN_PAT.match(s) or ENVELOPE_PAT.match(s):
            idx += 1
            continue
        break
    core = " ".join(ln.strip() for ln in lines[idx:] if ln.strip())
    core_norm = normalize_text(core)
    return core_norm[:max_len] if max_len else core_norm  # 默认全长核心指纹（GPT复审#1：48字前缀在公共接棒头下碰撞）


def new_client_message_id(prefix: str = "porta-m") -> str:
    rand_hex = "".join(random.choice("0123456789abcdef") for _ in range(6))
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{rand_hex}"


# ── 数据模型（严格对齐 v1.0 §2） ──────────────────────────────────────────

@dataclass
class UnifiedCandidate:
    leg: str
    hostId: str
    id: str
    alias: Optional[str]
    display_title: str
    title: str
    role: str
    cwd: str
    status: str
    lastActivity: str
    busySecondsAgo: Optional[int]
    reachable: bool
    unusableReason: Optional[str]
    truncated: bool
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def merge_key(self) -> Tuple[str, str, str]:
        return (self.leg, self.hostId, self.id)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EnumerateResult:
    ok: bool
    code: int
    hostId: str
    candidates: List[UnifiedCandidate]
    truncated: bool
    total_raw: int
    error: str = ""
    detail: str = ""


@dataclass
class VerifyResult:
    ok: bool
    code: int
    candidate: Optional[UnifiedCandidate]
    reason: str
    detail: str


@dataclass
class ResolveResult:
    ok: bool
    code: int
    candidate: Optional[UnifiedCandidate]
    candidates: List[UnifiedCandidate]
    matched_by: str
    detail: str
    truncated: bool = False


# ── 薄静态登记册（四层身份保护 · 层 1 & 层 2） ────────────────────────────

@dataclass
class RegistryEntry:
    conv_id: str
    host_id: str = "antigravity"
    alias: Optional[str] = None               # 人工别名（永不被同步覆写）
    display_title: str = ""                   # 上次同步原标题
    purpose: str = ""
    role: str = "peer"                        # peer | unknown | protected
    expected_project_id: Optional[str] = None
    allow_peer_inject: bool = False           # 白名单开关
    bound: bool = False                       # 是否为默认绑定协作窗


class PortaRegistry:
    """只读/薄静态登记册：按 (host_id, conv_id) 索引，不持久化实时 status/stepCount。"""

    def __init__(
        self,
        entries: Optional[Sequence[RegistryEntry]] = None,
        protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
    ) -> None:
        self._entries: List[RegistryEntry] = list(entries or [])
        self.protected_patterns: Tuple[str, ...] = tuple(
            normalize_text(p) for p in protected_patterns if str(p or "").strip()
        )

    @classmethod
    def from_file(cls, path: str) -> "PortaRegistry":
        if not path or not os.path.isfile(path):
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        porta_cfg = raw.get("porta", raw) if isinstance(raw, dict) else {}
        entries: List[RegistryEntry] = []
        for row in porta_cfg.get("entries", []):
            if not isinstance(row, dict) or not row.get("conv_id"):
                continue
            entries.append(
                RegistryEntry(
                    conv_id=str(row["conv_id"]).strip(),
                    host_id=str(row.get("host_id") or "antigravity").strip(),
                    alias=(str(row["alias"]).strip() or None) if row.get("alias") else None,
                    display_title=str(row.get("display_title") or ""),
                    purpose=str(row.get("purpose") or ""),
                    role=str(row.get("role") or "peer"),
                    expected_project_id=(
                        str(row["expected_project_id"]).strip()
                        if row.get("expected_project_id")
                        else None
                    ),
                    allow_peer_inject=bool(row.get("allow_peer_inject", False)),
                    bound=bool(row.get("bound", False)),
                )
            )
        pats = porta_cfg.get("protected_workspace_patterns")
        return cls(
            entries=entries,
            protected_patterns=pats if isinstance(pats, list) else DEFAULT_PROTECTED_PATTERNS,
        )

    def get(self, host_id: str, conv_id: str) -> Optional[RegistryEntry]:
        for e in self._entries:
            if e.conv_id == conv_id and (e.host_id == host_id or e.host_id == "*"):
                return e
        return None

    def find_by_alias(self, host_id: str, alias: str) -> List[RegistryEntry]:
        want = normalize_text(alias)
        if not want:
            return []
        return [
            e for e in self._entries
            if e.alias and normalize_text(e.alias) == want
            and (e.host_id == host_id or e.host_id == "*")
        ]

    def bound_entries(self, host_id: str) -> List[RegistryEntry]:
        return [
            e for e in self._entries
            if e.bound and (e.host_id == host_id or e.host_id == "*")
        ]

    def is_protected_workspace(self, cwd: str, project_name: str) -> bool:
        hay_cwd = normalize_text(cwd)
        hay_proj = normalize_text(project_name)
        for pat in self.protected_patterns:
            if pat and (pat in hay_cwd or pat in hay_proj):
                return True
        return False


# ── HTTP 客户端（可注入 Mock 单测） ─────────────────────────────────────────

class PortaHttpError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class PortaHttpClient:
    """基于标准库 urllib 的 Porta 代理轻量客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_PORTA_BASE,
        target_app: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.target_app = target_app.strip()
        self.timeout = timeout

    def _headers(self, json_body: bool = False) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if json_body:
            h["Content-Type"] = "application/json; charset=utf-8"
        if self.target_app:
            h["x-porta-target-app"] = self.target_app
        return h

    def _request_json(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, Dict[str, Any]]:
        url = self.base_url + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(json_body=payload is not None), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw.strip() else {}
                return int(resp.status), parsed if isinstance(parsed, dict) else {"data": parsed}
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise PortaHttpError(f"HTTP {exc.code} {method} {path}: {body}", status=exc.code) from exc
        except Exception as exc:
            raise PortaHttpError(f"连接 Porta 失败 {method} {path}: {exc}", status=0) from exc

    def get_health(self) -> Dict[str, Any]:
        _, data = self._request_json("GET", "/api/health")
        return data

    def get_conversations(self) -> Dict[str, Any]:
        _, data = self._request_json("GET", "/api/conversations")
        return data

    def get_steps_tail(self, conv_id: str, tail: int = 12) -> Dict[str, Any]:
        safe_id = urllib.parse.quote(conv_id, safe="")
        _, data = self._request_json("GET", f"/api/conversations/{safe_id}/steps?tail={int(tail)}")
        return data

    def post_message(
        self, conv_id: str, text: str, client_message_id: str
    ) -> Tuple[int, Dict[str, Any]]:
        safe_id = urllib.parse.quote(conv_id, safe="")
        payload = {
            "items": [{"text": text}],
            "clientMessageId": client_message_id,
        }
        return self._request_json("POST", f"/api/conversations/{safe_id}/messages", payload=payload)


# ── Porta 腿适配器主体 ──────────────────────────────────────────────────────

class PortaAdapter:
    """统一选窗器 · Porta（Antigravity）腿适配器。"""

    LEG = "porta"

    def __init__(
        self,
        client: Optional[PortaHttpClient] = None,
        registry: Optional[PortaRegistry] = None,
        busy_window_sec: int = DEFAULT_BUSY_WINDOW_SEC,
        require_whitelist: bool = False,
        now_fn: Callable[[], float] = _utc_now_ts,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or PortaHttpClient()
        self.registry = registry or PortaRegistry()
        self.busy_window_sec = int(busy_window_sec)
        self.require_whitelist = bool(require_whitelist)
        self._now = now_fn
        self._sleep = sleep_fn

    # ── 1. 桥存活闸 ────────────────────────────────────────────────────────

    def check_health(self) -> Tuple[bool, str, str]:
        """返回 (ok, host_id, detail)。必须 status=='ok' 且至少 1 个存活 LS。"""
        try:
            health = self.client.get_health()
        except PortaHttpError as exc:
            return False, "antigravity", str(exc)

        if health.get("status") != "ok":
            return False, "antigravity", f"Porta health 状态异常: {health.get('status')!r}"

        servers = health.get("languageServers")
        if not isinstance(servers, list) or len(servers) == 0:
            return False, "antigravity", "Porta 已启动但无存活 Antigravity Language Server 实例"

        app_dirs = sorted({str(s.get("appDataDir") or "antigravity") for s in servers if isinstance(s, dict)})
        host_id = "+".join(app_dirs) if app_dirs else "antigravity"
        return True, host_id, f"ok (languageServers={len(servers)})"

    # ── 2. 枚举与归一 ──────────────────────────────────────────────────────

    def _normalize_entry(
        self,
        conv_id: str,
        raw: Dict[str, Any],
        host_id: str,
        truncated: bool,
        now_ts: float,
    ) -> UnifiedCandidate:
        ann = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
        meta_obj = raw.get("trajectoryMetadata") if isinstance(raw.get("trajectoryMetadata"), dict) else {}

        # 标题读 annotations.title，备 summary（实测顶层无 title 键）
        display_title = str(ann.get("title") or raw.get("summary") or "").strip()

        # 工作区 URI：workspaces[0].workspaceFolderAbsoluteUri 或 trajectoryMetadata.workspaceUris[0]
        raw_uri = ""
        ws_list = raw.get("workspaces")
        if isinstance(ws_list, list) and ws_list:
            first_ws = ws_list[0]
            if isinstance(first_ws, dict) and first_ws.get("workspaceFolderAbsoluteUri"):
                raw_uri = str(first_ws["workspaceFolderAbsoluteUri"])
        if not raw_uri:
            ws_uris = meta_obj.get("workspaceUris")
            if isinstance(ws_uris, list) and ws_uris:
                raw_uri = str(ws_uris[0])
        cwd = decode_file_uri(raw_uri)

        project_name = str(raw.get("projectName") or "").strip()
        project_id = str(meta_obj.get("projectId") or "").strip()
        raw_status = str(raw.get("status") or "").strip()
        archived = bool(ann.get("archived", False))
        disk_only = bool(raw.get("_diskOnly", False)) or raw_status == STATUS_UNLOADED
        protected = self.registry.is_protected_workspace(cwd, project_name)

        # 计算 busySecondsAgo（基于 lastUserInputTime）
        last_input_ts = parse_iso_utc(raw.get("lastUserInputTime"))
        busy_sec: Optional[int] = None
        if last_input_ts is not None:
            busy_sec = max(0, int(now_ts - last_input_ts))

        # 登记册映射（只读合并稳定字段：alias / role / allow_peer_inject / expected_project_id）
        reg = self.registry.get(host_id, conv_id)
        alias = reg.alias if reg else None
        title = alias or display_title or conv_id[:8]

        # 判活与可达性状态机
        reachable = True
        unusable_reason: Optional[str] = None
        if archived:
            status = "archived"
            reachable = False
            unusable_reason = "archived"
        elif disk_only:
            status = "unloaded"
            reachable = False
            unusable_reason = "unloaded_disk_only"
        elif protected:
            status = "idle" if raw_status == STATUS_IDLE else ("running" if raw_status == STATUS_RUNNING else "unknown")
            reachable = False
            unusable_reason = "workspace_protected"
        elif raw_status == STATUS_IDLE:
            if busy_sec is not None and busy_sec < self.busy_window_sec:
                status = "busy_recent"
            else:
                status = "idle"
        elif raw_status == STATUS_RUNNING:
            status = "running"
        else:
            status = "unknown"
            reachable = False
            unusable_reason = f"unknown_status:{raw_status or 'empty'}"

        # 角色类别只对合格未标注行推导（GPT复审#2：默认peer是类别不是许可）；
        # 登记册显式类目始终优先；不合格行（保护/归档/未知态）＝unknown，资格闸另行拒绝。
        role = reg.role if reg else ("peer" if unusable_reason is None else "unknown")

        last_activity = str(raw.get("lastModifiedTime") or raw.get("createdTime") or "")
        return UnifiedCandidate(
            leg=self.LEG,
            hostId=host_id,
            id=conv_id,
            alias=alias,
            display_title=display_title,
            title=title,
            role=role,
            cwd=cwd,
            status=status,
            lastActivity=last_activity,
            busySecondsAgo=busy_sec,
            reachable=reachable,
            unusableReason=unusable_reason,
            truncated=truncated,
            meta={
                "stepCount": int(raw.get("stepCount") or 0),
                "createdTime": str(raw.get("createdTime") or ""),
                "projectName": project_name,
                "projectId": project_id,
                "trajectoryId": str(raw.get("trajectoryId") or ""),
                "rootConversationId": str(meta_obj.get("rootConversationId") or conv_id),
                "rawStatus": raw_status,
                "lastUserInputTime": str(raw.get("lastUserInputTime") or ""),
                "archived": archived,
                "diskOnly": disk_only,
                "protectedWorkspace": protected,
                "allowPeerInject": bool(reg.allow_peer_inject) if reg else False,
                "expectedProjectId": reg.expected_project_id if reg else None,
                "registered": reg is not None,
                "lastActivitySource": "porta.lastModifiedTime(UTC)",
            },
        )

    def enumerate(self) -> EnumerateResult:
        """枚举 Porta 全部候选（保留不可投原因供 verify_target / 诊断使用）。"""
        ok, host_id, detail = self.check_health()
        if not ok:
            return EnumerateResult(
                ok=False, code=EXIT_ENV, hostId=host_id, candidates=[],
                truncated=False, total_raw=0, error="porta_unhealthy", detail=detail,
            )

        try:
            resp = self.client.get_conversations()
        except PortaHttpError as exc:
            return EnumerateResult(
                ok=False, code=EXIT_ENV, hostId=host_id, candidates=[],
                truncated=False, total_raw=0, error="conversations_fetch_failed", detail=str(exc),
            )

        summaries = resp.get("trajectorySummaries")
        if not isinstance(summaries, dict):
            return EnumerateResult(
                ok=False, code=EXIT_ENV, hostId=host_id, candidates=[],
                truncated=False, total_raw=0, error="invalid_payload",
                detail="Porta /api/conversations 缺少 trajectorySummaries 字典",
            )

        total_raw = len(summaries)
        truncated = total_raw >= PORTA_MAX_CONVERSATIONS
        now_ts = self._now()

        cands: List[UnifiedCandidate] = []
        for conv_id, obj in summaries.items():
            if not isinstance(obj, dict):
                continue
            cands.append(self._normalize_entry(str(conv_id), obj, host_id, truncated, now_ts))

        # 按 lastActivity 降序（仅作展示排序，绝不用于自动挑选！）
        cands.sort(key=lambda c: (parse_iso_utc(c.lastActivity) or 0.0, c.id), reverse=True)
        return EnumerateResult(
            ok=True, code=EXIT_OK, hostId=host_id, candidates=cands,
            truncated=truncated, total_raw=total_raw,
        )

    # ── 3. 用户 §12 卡片预排除＋重名副标生成 ──────────────────────────────

    @staticmethod
    def filter_for_card(
        candidates: Sequence[UnifiedCandidate],
        show_archived: bool = False,
    ) -> List[UnifiedCandidate]:
        """用户 v1.0 §12 定调：组卡前脚本预排除归档/冷/受保护等不可投项，活跃集镜像侧边栏。"""
        out: List[UnifiedCandidate] = []
        for c in candidates:
            if c.reachable:
                out.append(c)
            elif show_archived and c.unusableReason == "archived":
                out.append(c)
        return out

    @staticmethod
    def build_card_options(
        candidates: Sequence[UnifiedCandidate],
        show_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """生成面向用户的卡片选项列表：标题大字优先，仅同标题重名行自动补消歧副标。"""
        active = PortaAdapter.filter_for_card(candidates, show_archived=show_archived)
        title_counts: Dict[str, int] = {}
        for c in active:
            key = normalize_text(c.title)
            title_counts[key] = title_counts.get(key, 0) + 1

        status_cn = {
            "idle": "空闲",
            "running": "思考中",
            "busy_recent": "刚有输入",
            "archived": "已归档",
        }

        options: List[Dict[str, Any]] = []
        for c in active:
            badge = status_cn.get(c.status, c.status)
            dup = title_counts.get(normalize_text(c.title), 0) > 1
            if dup:
                ws_short = os.path.basename(c.cwd) if c.cwd else (c.meta.get("projectName") or "无工作区")
                short_time = c.lastActivity[5:16].replace("T", " ") if len(c.lastActivity) >= 16 else c.lastActivity
                subtitle = f"{badge} · {short_time} · {ws_short} · #{c.id[:4]}"
            else:
                subtitle = badge
            options.append({
                "label": c.title,
                "subtitle": subtitle,
                "mergeKey": list(c.merge_key),
                "id": c.id,
                "leg": c.leg,
                "status": c.status,
                "duplicatedTitle": dup,
            })
        return options

    # ── 4. 统一投前校验 verify_target（四层保护核心闸） ────────────────────

    def verify_target(
        self,
        conv_id: str,
        enum_result: Optional[EnumerateResult] = None,
        *,
        allow_protected: bool = False,
        allow_running: bool = False,
        force_busy: bool = False,
        require_whitelist: Optional[bool] = None,
    ) -> VerifyResult:
        """所有入口（显式 ID/别名/关键字/卡片选中）必须复用的同一投前校验。"""
        er = enum_result if enum_result is not None else self.enumerate()
        if not er.ok:
            return VerifyResult(False, er.code, None, er.error or "porta_unhealthy", er.detail)

        want = (conv_id or "").strip()
        if not want:
            return VerifyResult(False, EXIT_TARGET_INVALID, None, "empty_id", "convId 为空")

        # 拦截误传 trajectoryId（验收场景 #9）
        for c in er.candidates:
            traj_id = c.meta.get("trajectoryId")
            if traj_id and traj_id == want and c.id != want:
                return VerifyResult(
                    False, EXIT_TARGET_INVALID, None, "trajectory_id_not_conv_id",
                    f"传入的是 trajectoryId（{want[:8]}…），不是可投递的 convId（正确 convId={c.id[:8]}…）",
                )

        hit = next((c for c in er.candidates if c.id == want), None)
        if hit is None:
            return VerifyResult(
                False, EXIT_TARGET_INVALID, None, "not_found",
                f"Porta 会话列表中找不到 convId={want}（可能已关闭、或超出 100 条窗口）",
            )

        # 排除：归档 / _diskOnly(UNLOADED) / 未知状态
        if hit.meta.get("archived"):
            return VerifyResult(False, EXIT_TARGET_INVALID, hit, "archived", f"会话已归档，禁止投递：{hit.title}")
        if hit.meta.get("diskOnly"):
            return VerifyResult(
                False, EXIT_TARGET_INVALID, hit, "unloaded_disk_only",
                f"会话仅在磁盘未载入内存（UNLOADED/_diskOnly），严禁直投：{hit.id[:8]}",
            )

        # 层 2：受保护工作区硬隔离（保护闸红线）
        if hit.meta.get("protectedWorkspace") and not allow_protected:
            return VerifyResult(
                False, EXIT_TARGET_INVALID, hit, "workspace_protected",
                f"命中受保护工作区（project={hit.meta.get('projectName') or hit.cwd}），未显式授权拒绝投递",
            )

        if hit.status == "unknown":
            return VerifyResult(
                False, EXIT_TARGET_INVALID, hit, hit.unusableReason or "unknown_status",
                f"会话状态未知（{hit.meta.get('rawStatus')}），拒投",
            )

        # 层 3：projectId 一致性校验（防抄错/漂移）
        expected_pid = hit.meta.get("expectedProjectId")
        actual_pid = hit.meta.get("projectId")
        if expected_pid and actual_pid and expected_pid != actual_pid:
            return VerifyResult(
                False, EXIT_TARGET_INVALID, hit, "project_id_mismatch",
                f"登记 expected_project_id={expected_pid} 与当前 projectId={actual_pid} 不符，拒投",
            )

        # 层 1：白名单检查（按配置开启）
        need_wl = self.require_whitelist if require_whitelist is None else require_whitelist
        if need_wl and not hit.meta.get("allowPeerInject"):
            return VerifyResult(
                False, EXIT_TARGET_INVALID, hit, "not_whitelisted",
                f"会话「{hit.title}」未加入 allow_peer_inject 白名单，需经用户选窗卡确认后再登记",
            )

        # 判活与忙窗检查（验收场景 #8；Porta 服务端本身不拦 RUNNING！）
        if hit.status == "running" and not allow_running:
            return VerifyResult(
                False, EXIT_BUSY, hit, "target_running",
                f"目标会话「{hit.title}」正在运行（RUNNING），Porta 服务端不拦并发，已拒绝注入",
            )
        if hit.status == "busy_recent" and not force_busy:
            return VerifyResult(
                False, EXIT_BUSY, hit, "busy_recent_input",
                f"目标会话「{hit.title}」{hit.busySecondsAgo}s 前刚有用户输入（<{self.busy_window_sec}s 忙窗），未加 --force 拒投",
            )

        return VerifyResult(True, EXIT_OK, hit, "verified", "ok")

    # ── 5. 关键字确定性子串匹配＋寻址解析 ─────────────────────────────────

    def match_keyword(
        self,
        keyword: str,
        candidates: Sequence[UnifiedCandidate],
        *,
        include_protected: bool = False,
    ) -> List[UnifiedCandidate]:
        """确定性子串匹配（大小写不敏感、Unicode 规范化、URI 已解码）；禁编辑距离/拼音直投。"""
        kw = normalize_text(keyword)
        if not kw:
            return []
        pool = [
            c for c in candidates
            if not c.meta.get("archived")
            and not c.meta.get("diskOnly")
            and (include_protected or not c.meta.get("protectedWorkspace"))
        ]
        seen = set()
        hits: List[UnifiedCandidate] = []
        for c in pool:
            if c.merge_key in seen:
                continue
            fields = (
                c.alias or "",
                c.display_title,
                c.cwd,
                str(c.meta.get("projectName") or ""),
            )
            matched = any(kw in normalize_text(f) for f in fields if f)
            if not matched and len(kw) >= 4 and c.id.lower().startswith(kw):
                matched = True
            if matched:
                seen.add(c.merge_key)
                hits.append(c)
        return hits

    def resolve(
        self,
        *,
        explicit_id: str = "",
        alias_or_name: str = "",
        keyword: str = "",
        allow_protected: bool = False,
        allow_running: bool = False,
        force_busy: bool = False,
    ) -> ResolveResult:
        """统一寻址入口：显式 ID > 精确别名 > 关键字 > 绑定窗 > 唯一活跃候选；守「绝不猜」。"""
        er = self.enumerate()
        if not er.ok:
            return ResolveResult(False, er.code, None, [], "health", er.detail, truncated=False)

        cands = er.candidates
        active = self.filter_for_card(cands, show_archived=False)

        # 入口 A：显式 convId（无论 truncated 与否，精确 ID 仍过同一 verify_target）
        if explicit_id.strip():
            vr = self.verify_target(
                explicit_id.strip(), er,
                allow_protected=allow_protected,
                allow_running=allow_running,
                force_busy=force_busy,
            )
            if vr.ok:
                return ResolveResult(True, EXIT_OK, vr.candidate, [vr.candidate], "explicit_id", "点名命中（convId）", er.truncated)
            return ResolveResult(False, vr.code, vr.candidate, active, "explicit_id", vr.detail, er.truncated)

        # 入口 B：精确别名（重复别名绝不取首条，返完整歧义且顺序无关！）
        if alias_or_name.strip():
            want = alias_or_name.strip()
            reg_hits = self.registry.find_by_alias(er.hostId, want)
            if len(reg_hits) > 1:
                ids = sorted({e.conv_id for e in reg_hits})
                dup_cands = [c for c in cands if c.id in ids]
                dup_cands.sort(key=lambda c: c.id)
                return ResolveResult(
                    False, EXIT_AMBIGUOUS, None, dup_cands, "alias_duplicate",
                    f"登记册存在 {len(ids)} 条重复别名「{want}」，拒绝顺序猜测", er.truncated,
                )
            if len(reg_hits) == 1:
                # 还要检查是否与另一条活跃会话的 display_title 碰撞（v1.0 §8 场景 #1）
                target_id = reg_hits[0].conv_id
                title_collisions = [
                    c for c in active
                    if c.id != target_id and normalize_text(c.display_title) == normalize_text(want)
                ]
                if title_collisions:
                    both = sorted(
                        [c for c in cands if c.id == target_id] + title_collisions,
                        key=lambda c: c.id,
                    )
                    return ResolveResult(
                        False, EXIT_AMBIGUOUS, None, both, "alias_title_collision",
                        f"别名「{want}」与另一条活跃会话标题同名，需用户确认", er.truncated,
                    )
                vr = self.verify_target(
                    target_id, er,
                    allow_protected=allow_protected,
                    allow_running=allow_running,
                    force_busy=force_busy,
                )
                if vr.ok:
                    return ResolveResult(True, EXIT_OK, vr.candidate, [vr.candidate], "alias", f"别名命中「{want}」", er.truncated)
                return ResolveResult(False, vr.code, vr.candidate, active, "alias", vr.detail, er.truncated)

            # 登记册无精确别名，退化为按标题精确匹配（非截断且唯一才放行）
            exact_title = [c for c in active if normalize_text(c.display_title) == normalize_text(want)]
            if len(exact_title) == 1:
                if er.truncated:
                    return ResolveResult(
                        False, EXIT_AMBIGUOUS, None, exact_title, "truncated_block",
                        "Porta 会话列表已达 100 条截断上限，禁止非 ID 唯一自动放行，请确认", True,
                    )
                vr = self.verify_target(
                    exact_title[0].id, er,
                    allow_protected=allow_protected,
                    allow_running=allow_running,
                    force_busy=force_busy,
                )
                if vr.ok:
                    return ResolveResult(True, EXIT_OK, vr.candidate, [vr.candidate], "exact_title", f"精确标题命中「{want}」", False)
                return ResolveResult(False, vr.code, vr.candidate, active, "exact_title", vr.detail, False)
            if len(exact_title) > 1:
                return ResolveResult(
                    False, EXIT_AMBIGUOUS, None, exact_title, "title_duplicate",
                    f"存在 {len(exact_title)} 条同名活跃对话「{want}」，需卡片点选", er.truncated,
                )
            return ResolveResult(False, EXIT_NO_TARGET, None, active, "alias_not_found", f"未找到别名或标题「{want}」", er.truncated)

        # 入口 C：关键字确定性子串匹配（v1.0 §5.2）
        if keyword.strip():
            # 先查受保护工作区是否命中（若是且未授权，给明确拦截提示而不是沉默 0 命中）
            all_hits = self.match_keyword(keyword, cands, include_protected=True)
            safe_hits = [c for c in all_hits if not c.meta.get("protectedWorkspace")]
            prot_hits = [c for c in all_hits if c.meta.get("protectedWorkspace")]

            if not safe_hits and prot_hits and not allow_protected:
                return ResolveResult(
                    False, EXIT_TARGET_INVALID, prot_hits[0], [], "keyword_protected_blocked",
                    f"关键字「{keyword}」仅命中受保护工作区（{prot_hits[0].meta.get('projectName') or prot_hits[0].cwd}），已拦截，派发 0 次",
                    er.truncated,
                )
            hits = all_hits if allow_protected else safe_hits
            if len(hits) == 0:
                return ResolveResult(False, EXIT_NO_TARGET, None, active, "keyword_no_match", f"关键字「{keyword}」无匹配活跃对话", er.truncated)
            if len(hits) > 1:
                return ResolveResult(
                    False, EXIT_AMBIGUOUS, None, hits, "keyword_ambiguous",
                    f"关键字「{keyword}」命中 {len(hits)} 条对话，请从命中子集卡片点选", er.truncated,
                )
            if er.truncated:
                return ResolveResult(
                    False, EXIT_AMBIGUOUS, None, hits, "truncated_block",
                    f"Porta 列表已达 {PORTA_MAX_CONVERSATIONS} 条截断上限，禁止关键字自动放行，请卡片确认", True,
                )
            vr = self.verify_target(
                hits[0].id, er,
                allow_protected=allow_protected,
                allow_running=allow_running,
                force_busy=force_busy,
            )
            if vr.ok:
                return ResolveResult(True, EXIT_OK, vr.candidate, [vr.candidate], "keyword_unique", f"关键字「{keyword}」唯一命中", False)
            return ResolveResult(False, vr.code, vr.candidate, hits, "keyword_unique", vr.detail, False)

        # 入口 D：未点名 → 优先检查唯一 bound 窗，否则检查唯一活跃候选
        bounds = self.registry.bound_entries(er.hostId)
        if len(bounds) == 1:
            vr = self.verify_target(
                bounds[0].conv_id, er,
                allow_protected=allow_protected,
                allow_running=allow_running,
                force_busy=force_busy,
            )
            if vr.ok:
                return ResolveResult(True, EXIT_OK, vr.candidate, [vr.candidate], "bound", "命中唯一绑定协作窗", er.truncated)
            return ResolveResult(False, vr.code, vr.candidate, active, "bound", f"绑定窗失效：{vr.detail}", er.truncated)

        if er.truncated:
            return ResolveResult(
                False, EXIT_AMBIGUOUS, None, active, "truncated_block",
                f"Porta 列表已达 {PORTA_MAX_CONVERSATIONS} 条截断上限，禁止无点名自动放行", True,
            )

        pool = [c for c in active if (not self.require_whitelist or c.meta.get("allowPeerInject"))]
        if len(pool) == 0:
            return ResolveResult(False, EXIT_NO_TARGET, None, [], "no_active", "没有可投递的活跃 Porta 对话", False)
        if len(pool) > 1:
            return ResolveResult(
                False, EXIT_AMBIGUOUS, None, pool, "multiple_active",
                f"存在 {len(pool)} 条可投活跃对话，绝不按最近活动猜，请点名或选卡", False,
            )
        vr = self.verify_target(
            pool[0].id, er,
            allow_protected=allow_protected,
            allow_running=allow_running,
            force_busy=force_busy,
        )
        if vr.ok:
            return ResolveResult(True, EXIT_OK, vr.candidate, [vr.candidate], "unique_active", "唯一活跃候选", False)
        return ResolveResult(False, vr.code, vr.candidate, pool, "unique_active", vr.detail, False)

    # ── 6. 投递＋投后 /steps?tail= 真证据核验 ──────────────────────────────

    @staticmethod
    def _extract_user_step_text(step: Dict[str, Any]) -> str:
        ui = step.get("userInput")
        if not isinstance(ui, dict):
            return ""
        parts: List[str] = []
        items = ui.get("items")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("text"):
                    parts.append(str(it["text"]))
        if not parts and ui.get("userResponse"):
            parts.append(str(ui["userResponse"]))
        return "\n".join(parts)

    @staticmethod
    def _extract_reply_text(step: Dict[str, Any]) -> str:
        pr = step.get("plannerResponse")
        if not isinstance(pr, dict):
            return ""
        return str(pr.get("response") or pr.get("modifiedResponse") or "").strip()

    def _inspect_delivery_steps(
        self,
        conv_id: str,
        pre_step: int,
        body_fingerprint: str,
        client_message_id: str,
        tail: int = 16,
    ) -> Dict[str, Any]:
        """查 /steps?tail= 核实正文指纹是否真的落盘（防旁人打字 stepCount 假阳性）以及是否已有新回复。"""
        data = self.client.get_steps_tail(conv_id, tail=tail)
        offset = int(data.get("offset") or 0)
        step_count = int(data.get("stepCount") or 0)
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []

        matched_user_idx: Optional[int] = None
        matched_cid = False
        matched_fp = False

        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            abs_idx = offset + i
            if abs_idx < pre_step:
                continue
            if step.get("type") != STEP_USER_INPUT:
                continue
            step_core = extract_body_fingerprint(self._extract_user_step_text(step))
            fp_ok = bool(body_fingerprint) and (step_core == body_fingerprint)  # 全长核心对称比对（GPT复审#1）
            cid_ok = bool(client_message_id) and (step.get("clientMessageId") == client_message_id)
            # 扫全批新增step：优先正文＋CID双命中；异CID同文在先不得break漏掉后面正确step（GPT桥复审#1）
            if fp_ok and cid_ok:
                matched_user_idx = abs_idx
                matched_fp = True
                matched_cid = True
                break
            if fp_ok and matched_user_idx is None:
                matched_user_idx = abs_idx
                matched_fp = True
                matched_cid = False  # 仅同文待核：桥层按 association_missing 分流，不产证据

        reply_idx: Optional[int] = None
        reply_text = ""
        if matched_user_idx is not None:
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                abs_idx = offset + i
                if abs_idx <= matched_user_idx:
                    continue
                if step.get("type") == STEP_PLANNER_RESPONSE and step.get("status") == STEP_STATUS_DONE:
                    rt = self._extract_reply_text(step)
                    if rt:
                        reply_idx = abs_idx
                        reply_text = rt

        return {
            "stepCount": step_count,
            "userStepIndex": matched_user_idx,
            "matchedBodyFingerprint": matched_fp,
            "matchedClientMessageId": matched_cid,
            "replyStepIndex": reply_idx,
            "replyHead": reply_text[:160],
            "hasReply": bool(reply_text),
        }

    def deliver(self, conv_id: str, text: str, **options) -> Tuple[int, Dict[str, Any]]:
        code, info = self._deliver_impl(conv_id, text, **options)
        if options.get("dry_run"):
            info.update(status=None, resultKnown=False)
            return code, info

        http = info.get("http") or 0
        landed = bool(info.get("matchedBodyFingerprint"))
        stage = info.get("stage")
        rejected = stage in {"validate", "binding_check", "final_reverify", "envelope_check"}
        rejected = rejected or (stage == "verify_target" and code != EXIT_ENV)
        rejected = rejected or (400 <= http < 500 and http != 408)
        info.update(receipt_tri.report(
            "porta", conv_id, landed=landed, accepted=True if 200 <= http < 300 else None,
            reply=bool(info.get("replyStepIndex") is not None),
            error=info.get("reason") if rejected else None, reason=info.get("reason") or "",
        ))
        if landed:
            info["verified"] = True
        return code, info

    def _deliver_impl(
        self,
        conv_id: str,
        text: str,
        *,
        peer_agent: str,
        dry_run: bool = False,
        allow_protected: bool = False,
        allow_running: bool = False,
        force_busy: bool = False,
        wait_for_reply: bool = True,
        poll_timeout_sec: float = DEFAULT_POLL_TIMEOUT_SEC,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        expected_host: Optional[str] = None,
        expected_role: Optional[str] = None,
        expected_cwd: Optional[str] = None,
        binding: Optional[str] = None,
        from_addr: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """投递前二次 verify_target → 强制 ORIGIN 幂等＋clientMessageId → 投后 /steps?tail= 真核验。
        expected_host/role/cwd＝选中键完整地址（GPT桥复审#2）：在真正用于 POST 的这次快照上终验，dry-run 同闸。"""
        if not (text or "").strip():
            return EXIT_ENV, {"ok": False, "code": EXIT_ENV, "stage": "validate", "reason": "empty_text", "at": _iso_stamp(self._now())}
        if not (peer_agent or "").strip():
            return EXIT_ENV, {"ok": False, "code": EXIT_ENV, "stage": "validate", "reason": "missing_peer_agent", "at": _iso_stamp(self._now())}

        # 投前二次复验（防选完卡片犹豫期间窗变 RUNNING/关闭/归档）；同一份枚举既供复验也供 POST（GPT桥复审#2）
        er = self.enumerate()

        # 投前现查绑定：有新窗口出现时不许默认继续投旧窗（2026-11-24 口径）
        _binding = (binding if binding is not None else os.environ.get("PORTA_BINDING", "")).strip()
        rows = [{"id": c.id, "last": c.meta.get("createdTime") or c.lastActivity or ""}
                for c in er.candidates if c.reachable]
        rows.sort(key=lambda row: row["last"], reverse=True)
        chk = binding_verdict(_binding, conv_id, rows)
        if not chk["ok"]:
            return EXIT_TARGET_INVALID, {
                "ok": False, "code": EXIT_TARGET_INVALID, "stage": "binding_check",
                "reason": chk["reason"], "newer": chk.get("newer"), "convId": conv_id,
                "leg": self.LEG, "at": _iso_stamp(self._now()),
            }

        vr = self.verify_target(
            conv_id,
            er,
            allow_protected=allow_protected,
            allow_running=allow_running,
            force_busy=force_busy,
        )
        if not vr.ok or vr.candidate is None:
            return vr.code, {
                "ok": False,
                "code": vr.code,
                "stage": "verify_target",
                "reason": vr.reason,
                "detail": vr.detail,
                "leg": self.LEG,
                "convId": conv_id,
                "truncated": er.truncated,
                "hostId": er.hostId,
                "at": _iso_stamp(self._now()),
            }

        cand = vr.candidate
        # 终验在真正用于 POST 的这次快照上（GPT桥复审#2）：完整选中键/角色/选窗cwd，dry-run 同闸
        if expected_host is not None and cand.hostId != expected_host:
            return EXIT_TARGET_INVALID, {"ok": False, "code": EXIT_TARGET_INVALID, "stage": "final_reverify",
                                          "reason": "host_mismatch", "detail": "POST前快照host已漂移", "leg": self.LEG,
                                          "convId": conv_id, "hostId": cand.hostId, "at": _iso_stamp(self._now())}
        if expected_role is not None:
            sel_role = expected_role.strip()
            fresh_role = (cand.role or "").strip()
            # 干跑真投共用slot角色门（GPT终审）：选中或快照身份未知→待证据2；非peer白名单或角色漂移→权威6
            if sel_role in ("", "unknown") or fresh_role in ("", "unknown"):
                return EXIT_ENV, {"ok": False, "code": EXIT_ENV, "stage": "final_reverify",
                                  "reason": "identity_missing", "detail": "选中或POST前快照身份未知，待补证据", "leg": self.LEG,
                                  "convId": conv_id, "at": _iso_stamp(self._now())}
            if sel_role != "peer" or fresh_role != sel_role:
                return EXIT_TARGET_INVALID, {"ok": False, "code": EXIT_TARGET_INVALID, "stage": "final_reverify",
                                              "reason": "role_mismatch", "detail": "选中非peer白名单或POST前快照角色已变更", "leg": self.LEG,
                                              "convId": conv_id, "at": _iso_stamp(self._now())}
        if expected_cwd is not None:
            import codex_picker_adapter as _cxa  # 路径键单一正本（UNC/大小写/分隔符同口径）
            # 显式空cwd也参加路径键比较（选中空cwd≠无约束，GPT第三轮#2）
            if _cxa.fs_path_key(cand.cwd or "") != _cxa.fs_path_key(expected_cwd):
                return EXIT_TARGET_INVALID, {"ok": False, "code": EXIT_TARGET_INVALID, "stage": "final_reverify",
                                              "reason": "cwd_drift", "detail": "POST前快照工作区已漂移", "leg": self.LEG,
                                              "convId": conv_id, "at": _iso_stamp(self._now())}
        sender = (from_addr if from_addr is not None else os.environ.get("KIT_FROM_ADDR", "")).strip()
        try:
            wrapped = msg_envelope.for_delivery(text, from_addr=sender,
                                                to_addr=msg_envelope.addr(self.LEG, cand.hostId, cand.id),
                                                peer_agent=peer_agent)
        except ValueError as exc:
            return EXIT_TARGET_INVALID, {"ok": False, "stage": "envelope_check", "reason": "invalid_envelope",
                                         "detail": str(exc), "convId": cand.id, "leg": self.LEG}
        body_fp = extract_body_fingerprint(wrapped)
        cid = new_client_message_id()
        pre_step = int(cand.meta.get("stepCount") or 0)

        base_receipt: Dict[str, Any] = {
            "at": _iso_stamp(self._now()),
            "leg": self.LEG,
            "hostId": cand.hostId,
            "convId": cand.id,
            "title": cand.title,
            "cwd": cand.cwd,
            "role": cand.role,
            "peerAgent": peer_agent,
            "clientMessageId": cid,
            "preStep": pre_step,
            "chars": len(wrapped),
            "truncated": cand.truncated,
        }

        if dry_run:
            return EXIT_OK, dict(
                base_receipt,
                ok=True,
                code=EXIT_OK,
                stage="dry_run",
                reason="dry_run_verified",
                bodyFingerprint=hashlib.sha1(body_fp.encode("utf-8")).hexdigest()[:16],  # 摘要而非全长正文（GPT桥复审#1建议）
                preview=wrapped[:240],
            )

        try:
            http_status, resp = self.client.post_message(cand.id, wrapped, cid)
        except PortaHttpError as exc:
            return EXIT_DELIVER_VERIFY_FAIL, dict(
                base_receipt,
                ok=False,
                code=EXIT_DELIVER_VERIFY_FAIL,
                stage="post_message",
                reason="http_error",
                http=exc.status,
                detail=str(exc),
            )

        if http_status < 200 or http_status >= 300:
            return EXIT_DELIVER_VERIFY_FAIL, dict(
                base_receipt,
                ok=False,
                code=EXIT_DELIVER_VERIFY_FAIL,
                stage="post_message",
                reason="non_2xx",
                http=http_status,
                detail=str(resp)[:200],
            )

        # 投后有界轮询：核 stepCount 增长 ＋ /steps?tail= 命中正文指纹（排除旁人打字假阳性）
        deadline = self._now() + max(0.0, float(poll_timeout_sec))
        last_check: Dict[str, Any] = {}
        latest_status = ""
        while True:
            try:
                last_check = self._inspect_delivery_steps(cand.id, pre_step, body_fp, cid)
                er_after = self.enumerate()
                if er_after.ok:
                    after_c = next((x for x in er_after.candidates if x.id == cand.id), None)
                    if after_c:
                        latest_status = after_c.status
            except PortaHttpError as exc:
                last_check = {"error": str(exc)}

            step_grew = int(last_check.get("stepCount") or 0) > pre_step
            fp_hit = bool(last_check.get("matchedBodyFingerprint"))
            has_reply = bool(last_check.get("hasReply"))

            if step_grew and fp_hit:
                if (not wait_for_reply) or (has_reply and latest_status in ("idle", "busy_recent", "")):
                    return EXIT_OK, dict(
                        base_receipt,
                        ok=True,
                        code=EXIT_OK,
                        stage="verified",
                        reason="delivered_and_replied" if has_reply else "delivered_confirmed",
                        http=http_status,
                        postStep=last_check.get("stepCount"),
                        userStepIndex=last_check.get("userStepIndex"),
                        matchedBodyFingerprint=True,
                        matchedClientMessageId=bool(last_check.get("matchedClientMessageId")),
                        replyStepIndex=last_check.get("replyStepIndex"),
                        replyHead=last_check.get("replyHead", ""),
                        postStatus=latest_status,
                    )

            if self._now() >= deadline:
                break
            self._sleep(poll_interval_sec)

        # 超时分流：已确认落盘且在跑 → 明确告知勿重投；未命中正文指纹（即使 stepCount 涨了）→ 判失败！
        step_grew = int(last_check.get("stepCount") or 0) > pre_step
        fp_hit = bool(last_check.get("matchedBodyFingerprint"))
        if step_grew and fp_hit:
            return EXIT_OK, dict(
                base_receipt,
                ok=True,
                code=EXIT_OK,
                stage="delivered_running",
                reason="delivered_still_running_do_not_retry",
                http=http_status,
                postStep=last_check.get("stepCount"),
                userStepIndex=last_check.get("userStepIndex"),
                matchedBodyFingerprint=True,
                matchedClientMessageId=bool(last_check.get("matchedClientMessageId")),
                postStatus=latest_status or "running",
                note="正文已确认落入会话且正在思考中（超过观察窗），切勿重投以免撞车",
            )

        return EXIT_DELIVER_VERIFY_FAIL, dict(
            base_receipt,
            ok=False,
            code=EXIT_DELIVER_VERIFY_FAIL,
            stage="post_verify",
            reason="fingerprint_not_found_in_steps" if step_grew else "step_count_unchanged",
            http=http_status,
            postStep=last_check.get("stepCount", pre_step),
            matchedBodyFingerprint=False,
            matchedClientMessageId=bool(last_check.get("matchedClientMessageId")),
            postStatus=latest_status,
            detail="stepCount 虽变化但 /steps?tail= 未命中本次正文指纹（可能为旁人同期输入）" if step_grew else "轮询超时且 stepCount 未增长",
        )
