"""Read-only Codex candidate discovery for the unified picker (design v1.0)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unicodedata
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import UUID

Address = tuple[str, str, str]
WorkspacePolicy = Callable[[Address, str], bool]
BUSY_WINDOW = 60
MAX_HEADER_BYTES = 1024 * 1024
ROLES = {"feishu_bot", "desktop", "delegated", "peer", "unknown"}


def label_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value or "").split()).casefold()


def _id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing thread ID")
    value = value.strip()
    try:
        return str(UUID(value))
    except ValueError:
        return value


def address(host_id: str, thread_id: str) -> Address:
    if not isinstance(host_id, str) or not host_id.strip():
        raise ValueError("missing host ID")
    return ("codex", host_id, _id(thread_id))


def _read_source(read):
    try:
        return read(), "ok"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, sqlite3.Error) as exc:
        return None, f"error:{type(exc).__name__}"


def _open_readonly(db: Path):
    db.stat()
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def _threads(home: Path, host_id: str) -> dict[Address, dict]:
    with closing(_open_readonly(home / "state_5.sqlite")) as con:
        cols = {col["name"] for col in con.execute("PRAGMA table_info(threads)")}
        ms = "updated_at_ms" if "updated_at_ms" in cols else "NULL AS updated_at_ms"
        rows = con.execute(
            "SELECT id, name, title, cwd, source, archived, updated_at, rollout_path, "
            f"{ms} FROM threads"
        ).fetchall()
    records = {address(host_id, row["id"]): dict(row) for row in rows}
    if len(records) != len(rows):
        raise ValueError("duplicate local thread address")
    return records


def _catalog(home: Path) -> dict[Address, dict]:
    with closing(_open_readonly(home / "sqlite" / "codex.db")) as con:
        rows = con.execute(
            "SELECT host_id, thread_id, display_title, cwd, source_kind, "
            "source_updated_at, missing_candidate, project_id FROM local_thread_catalog"
        ).fetchall()
    records = {address(row["host_id"], row["thread_id"]): dict(row) for row in rows}
    if len(records) != len(rows):
        raise ValueError("duplicate desktop thread address")
    return records


def _register(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        raise ValueError("invalid registry structure")
    for row in payload["lines"]:
        if not isinstance(row, dict):
            raise ValueError("invalid registry entry")
        _id(row.get("thread_id"))
        if row.get("leg", "codex") != "codex":
            raise ValueError("invalid registry leg")
        for field in ("alias", "legacy_alias", "别名", "display_title", "用途", "kind"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ValueError(f"invalid registry field: {field}")
        if "hostId" in row:
            address(row["hostId"], row["thread_id"])
    return payload


def _is_cloud(catalog_or_reg: dict) -> bool:
    kind = catalog_or_reg.get("source_kind") or catalog_or_reg.get("kind") or ""
    return kind.casefold() == "chatgpt"


def _project_index(home: Path) -> tuple[dict, list]:
    """Codex 桌面项目分组索引（用户 2026-11-22 截图＋说明，本机实读 state_5.sqlite 验证）。
    name_by_pid：projects.id(UUID) 与 metadata.codex.chatGptBacking.projectId(g-p-…)→项目名；
    roots：project_roots.path→项目名（本地对话按 cwd 落根归组）。只读，失败降级返回空。"""
    name_by_pid: dict = {}
    roots: list = []
    try:
        with closing(_open_readonly(home / "state_5.sqlite")) as con:
            for row in con.execute("SELECT id, name, metadata FROM projects"):
                pid, name = row["id"], row["name"]
                if not name:
                    continue
                name_by_pid[pid] = name
                try:
                    meta = json.loads(row["metadata"] or "{}")
                    backing = json.loads(meta.get("codex.chatGptBacking", "{}"))
                    gpid = backing.get("projectId")
                    if gpid:
                        name_by_pid[gpid] = name
                except (ValueError, TypeError, AttributeError):
                    pass
            for row in con.execute("SELECT project_id, path FROM project_roots"):
                nm = name_by_pid.get(row["project_id"])
                if nm and row["path"]:
                    roots.append((row["path"], nm))
    except (OSError, ValueError, sqlite3.Error):
        pass
    return name_by_pid, roots


def _norm_path(p: str) -> str:
    p = (p or "").replace("?", "").replace("/", chr(92))
    return p.lstrip(chr(92)).rstrip(chr(92)).casefold()


def _project_of(cat_pid, cwd, name_by_pid, roots) -> str:
    """显式 catalog.project_id 优先；否则 cwd 最长前缀落 project_roots 归组（镜像桌面侧边栏）。"""
    if cat_pid and cat_pid in name_by_pid:
        return name_by_pid[cat_pid]
    cw = _norm_path(cwd)
    if not cw:
        return ""
    best, best_len = "", -1
    sep = chr(92)
    for path, nm in roots:
        rp = _norm_path(path)
        if rp and (cw == rp or cw.startswith(rp + sep)) and len(rp) > best_len:
            best, best_len = nm, len(rp)
    return best


@dataclass
class Snapshot:
    host_id: str
    home: Path
    registry: dict
    threads: dict[Address, dict]
    catalog: dict[Address, dict]
    bindings: dict[Address, dict]
    sources: dict[str, str]

    @property
    def keys(self) -> set[Address]:
        return self.threads.keys() | self.catalog.keys() | self.bindings.keys()

    @property
    def truncated(self) -> bool:
        return any(status != "ok" for status in self.sources.values())


def read_snapshot(*, host_id: str, codex_home: Path, registry_path: Path) -> Snapshot:
    """Read the three sources in full. A failed source is distinct from an empty one."""
    if not isinstance(host_id, str) or not host_id.strip():
        raise ValueError("missing host ID")
    home = Path(codex_home).expanduser().absolute()
    threads, state_status = _read_source(lambda: _threads(home, host_id))
    catalog, cat_status = _read_source(lambda: _catalog(home))
    registry, reg_status = _read_source(lambda: _register(Path(registry_path)))
    threads, catalog = threads or {}, catalog or {}
    registry = registry or {"lines": []}

    # A legacy row has no host. Bind only when the complete inventories identify one address.
    current_ids = defaultdict(list)
    for key in threads.keys() | catalog.keys():
        current_ids[key[2]].append(key)

    by_address = defaultdict(list)
    unbound = 0
    for row in registry["lines"]:
        if "hostId" in row:
            key = address(row["hostId"], row["thread_id"])
        else:
            candidates = [
                key for key in current_ids[_id(row["thread_id"])]
                if _is_cloud(catalog.get(key, {})) == _is_cloud(row)
            ]
            if state_status != "ok" or cat_status != "ok" or len(candidates) != 1:
                unbound += 1
                continue
            key = candidates[0]
        by_address[key].append(row)

    bindings = {key: rows[0] for key, rows in by_address.items() if len(rows) == 1}
    if unbound or len(bindings) != len(by_address):
        reg_status = "unbound_or_duplicate"
    return Snapshot(
        host_id, home, registry, threads, catalog, bindings,
        {"threads": state_status, "catalog": cat_status, "registry": reg_status},
    )


def _legacy_alias(row: dict) -> str:
    if "legacy_alias" in row or "alias" in row:
        return row.get("legacy_alias") or ""
    return row.get("别名") or ""


def _display_title(snapshot: Snapshot, key: Address) -> str:
    thread = snapshot.threads.get(key, {})
    catalog = snapshot.catalog.get(key, {})
    return (
        catalog.get("display_title") or thread.get("name") or thread.get("title")
        or snapshot.bindings.get(key, {}).get("display_title") or f"Codex {key[2][:8]}"
    )


def matching_labels(snapshot: Snapshot, key: Address) -> tuple[str, ...]:
    thread = snapshot.threads.get(key, {})
    binding = snapshot.bindings.get(key, {})
    return (
        binding.get("alias") or "", _legacy_alias(binding), _display_title(snapshot, key),
        thread.get("name") or "", thread.get("title") or "",
    )


def _activity(snapshot: Snapshot, key: Address) -> tuple[float | None, str | None]:
    thread = snapshot.threads.get(key, {})
    if thread.get("updated_at_ms"):
        return thread["updated_at_ms"] / 1000, "threads.updated_at_ms"
    if thread.get("updated_at"):
        return thread["updated_at"], "threads.updated_at"
    updated = snapshot.catalog.get(key, {}).get("source_updated_at")
    return (updated, "catalog.source_updated_at") if updated else (None, None)


@dataclass(frozen=True)
class VerifiedTarget:
    reason: str | None = None
    rollout: Path | None = None
    mtime: float | None = None


def _inspect_rollout(path: Path, root: Path, thread_id: str) -> VerifiedTarget:
    suffix = f"-{thread_id}.jsonl"
    if not path.is_absolute() or not path.name.casefold().startswith("rollout-"):
        return VerifiedTarget("rollout_identity_mismatch")
    if not path.name.casefold().endswith(suffix):
        return VerifiedTarget("rollout_identity_mismatch")
    try:
        real_path = path.resolve(strict=True)
        if (not real_path.is_relative_to(root) or not real_path.name.casefold().endswith(suffix)
                or not real_path.is_file()):
            return VerifiedTarget("rollout_identity_mismatch")
        with real_path.open("rb") as f:
            line = f.readline(MAX_HEADER_BYTES + 1)
            mtime = os.fstat(f.fileno()).st_mtime
    except FileNotFoundError:
        return VerifiedTarget("missing_rollout")
    except (OSError, RuntimeError):
        return VerifiedTarget("unreadable_rollout")

    try:
        header = json.loads(line) if len(line) <= MAX_HEADER_BYTES else None
        if not isinstance(header, dict) or header.get("type") != "session_meta":
            return VerifiedTarget("rollout_identity_mismatch")
        payload = header.get("payload")
        header_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(header_id, str) or not header_id.strip():
            return VerifiedTarget("rollout_identity_mismatch")
        if str(UUID(header_id)) != thread_id:
            return VerifiedTarget("rollout_identity_mismatch")
    except ValueError:
        return VerifiedTarget("rollout_identity_mismatch")
    return VerifiedTarget(rollout=real_path, mtime=mtime)


def _rollout(home: Path, key: Address, thread: dict) -> VerifiedTarget:
    try:
        thread_id = str(UUID(key[2]))
    except ValueError:
        # The CLI may interpret a non-UUID positional argument as a thread name.
        return VerifiedTarget("unsupported_thread_id")
    configured_root = home / "sessions"
    try:
        root = configured_root.resolve(strict=True)
    except FileNotFoundError:
        return VerifiedTarget("missing_rollout")
    except (OSError, RuntimeError):
        return VerifiedTarget("unreadable_rollout")

    primary = VerifiedTarget("missing_rollout")
    if thread.get("rollout_path"):
        raw_path = str(thread["rollout_path"])
        if os.name == "nt" and raw_path.startswith("\\\\?\\") and not raw_path[4:].startswith("UNC\\"):
            raw_path = raw_path[4:]
        registered = Path(raw_path)
        if registered.is_absolute():
            registered = Path(os.path.abspath(registered))
        if registered.is_relative_to(configured_root) or registered.is_relative_to(root):
            primary = _inspect_rollout(registered, root, thread_id)
            if primary.rollout is not None:
                return primary
        else:
            primary = VerifiedTarget("rollout_identity_mismatch")

    # Refuse multiple same-ID filenames before opening a recovered rollout.
    try:
        paths = sorted(root.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    except OSError:
        return VerifiedTarget("unreadable_rollout")
    if len(paths) > 1:
        return VerifiedTarget("ambiguous_rollout")
    if paths:
        return _inspect_rollout(paths[0], root, thread_id)
    return primary


def verify_target(
    snapshot: Snapshot, key: Address, *, workspace_allowed: WorkspacePolicy | None = None
) -> VerifiedTarget:
    """Check this exact address against the most recent snapshot. No name lookup or writes."""
    thread = snapshot.threads.get(key)
    catalog = snapshot.catalog.get(key, {})
    binding = snapshot.bindings.get(key, {})
    if key[0] != "codex":
        return VerifiedTarget("host_mismatch")
    if _is_cloud(catalog) or (thread is None and _is_cloud(binding)):
        return VerifiedTarget("cloud_no_rollout")
    if key[1] != snapshot.host_id:
        return VerifiedTarget("host_mismatch")
    if snapshot.sources["threads"] != "ok":
        return VerifiedTarget("state_unavailable")
    if thread is None:
        return VerifiedTarget("no_local_record")
    if thread.get("archived"):
        return VerifiedTarget("archived")
    if (thread.get("source") or "").casefold() == "exec":
        return VerifiedTarget("source_exec")
    cwd = thread.get("cwd") or ""
    if workspace_allowed is not None and (not cwd or not workspace_allowed(key, cwd)):
        return VerifiedTarget("workspace_protected")
    return _rollout(snapshot.home, key, thread)


def delivery_state(codex_home: Path, thread_id: str) -> tuple[str, int]:
    """Read optional desktop stores for the last turn and pending queue, without changing them."""
    turn = ""
    pending = 0
    try:
        with closing(_open_readonly(codex_home / "thread_history_1.sqlite")) as db:
            row = db.execute(
                "SELECT status FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if row:
                turn = str(row[0] or "")
    except (OSError, sqlite3.Error):
        pass
    try:
        with closing(_open_readonly(codex_home / "queue_1.sqlite")) as db:
            pending = db.execute("SELECT count(*) FROM queued_items WHERE thread_id=?", (thread_id,)).fetchone()[0]
    except (OSError, sqlite3.Error):
        pass
    return turn, pending


def registration_lines(snapshot: Snapshot) -> list[dict]:
    """Refresh machine fields without transferring human data between addresses."""
    lines = []
    for key in sorted(snapshot.keys):
        old = snapshot.bindings.get(key, {})
        thread = snapshot.threads.get(key, {})
        cat = snapshot.catalog.get(key, {})
        title = _display_title(snapshot, key)
        alias, legacy = old.get("alias") or "", _legacy_alias(old)
        verdict = verify_target(snapshot, key)
        updated, _ = _activity(snapshot, key)
        lines.append({
            "leg": key[0], "hostId": key[1], "thread_id": key[2],
            "alias": alias, "legacy_alias": legacy, "display_title": title,
            "别名": alias or legacy or title,  # legacy presentation field, never identity
            "用途": old.get("用途") or "",
            "kind": cat.get("source_kind") or thread.get("source") or old.get("kind") or "",
            "已归档": bool(thread.get("archived")),
            "可投": verdict.reason is None,
            "unusableReason": verdict.reason,
            "cwd": thread.get("cwd") or cat.get("cwd") or "",
            "最后活动": updated,
        })
    return lines


def enumerate(
    *, host_id: str, codex_home: Path, registry_path: Path,
    workspace_allowed: WorkspacePolicy | None = None, roles: dict[Address, str] | None = None,
) -> dict:
    """Return UnifiedCandidate rows plus completeness, including an empty degraded result."""
    snapshot = read_snapshot(
        host_id=host_id, codex_home=codex_home, registry_path=registry_path
    )
    candidates = []
    now = time.time()
    roles = roles or {}
    name_by_pid, proj_roots = _project_index(snapshot.home)
    for key in sorted(snapshot.keys):
        thread = snapshot.threads.get(key, {})
        cat = snapshot.catalog.get(key, {})
        binding = snapshot.bindings.get(key, {})
        verdict = verify_target(snapshot, key, workspace_allowed=workspace_allowed)
        busy_age = max(0.0, now - verdict.mtime) if verdict.mtime is not None else None
        status = {"archived": "archived", "cloud_no_rollout": "cloud"}.get(
            verdict.reason, "unknown"
        )
        if busy_age is not None and busy_age < BUSY_WINDOW:
            status = "busy_recent"
        updated, activity_source = _activity(snapshot, key)
        try:
            activity = datetime.fromtimestamp(updated, timezone.utc).isoformat() if updated else None
        except (ValueError, OverflowError, OSError):
            activity, activity_source = None, None
        title = _display_title(snapshot, key)
        alias = binding.get("alias") or ""
        role = roles.get(key) or ("peer" if verdict.reason is None else "unknown")  # 类别只对合格未标注行推导（GPT复审#2）；显式roles优先；保护/归档行＝unknown，资格闸仍拒
        candidates.append({
            "leg": key[0],
            "hostId": key[1],
            "id": key[2],
            "alias": alias,
            "display_title": title,
            "title": alias or title,
            "role": role if role in ROLES else "unknown",
            "cwd": thread.get("cwd") or cat.get("cwd") or "",
            "status": status,
            "lastActivity": activity,
            "busySecondsAgo": busy_age,
            "reachable": verdict.reason is None,
            "unusableReason": verdict.reason,
            "truncated": snapshot.truncated,
            "meta": {
                "source": thread.get("source"),
                "sourceKind": cat.get("source_kind"),
                "lastActivitySource": activity_source,
                "missingCandidate": bool(cat.get("missing_candidate")),
                "inCatalog": key in snapshot.catalog,
                "legacyAlias": _legacy_alias(binding),
                "projectId": cat.get("project_id") or "",
                "projectName": _project_of(cat.get("project_id"), thread.get("cwd") or cat.get("cwd") or "", name_by_pid, proj_roots),
            },
        })
    return {
        "candidates": candidates,
        "sources": snapshot.sources,
        "paginationComplete": not snapshot.truncated,
        "truncated": snapshot.truncated,
    }

# ── 共享受保护工作区策略（picker.py 与 reach_gpt.py 复用同一口径，默认拦截受保护工作区） ──
import urllib.parse as _urlparse

_ENV_PROTECTED = tuple(
    p.strip() for p in os.environ.get("PROTECTED_WORKSPACES", "").split(",") if p.strip()
)
PROTECTED_WORKSPACE_PATTERNS = _ENV_PROTECTED or ("protected-workspace", "private-zone")


def default_workspace_allowed(addr, cwd: str) -> bool:
    """统一工作区授权：URL 解码＋label_key 规范化后，命中受保护模式即拒。

    单一真相源——统一选窗器编排层与 reach_gpt 实际提交前都调它，口径一致。
    """
    c = label_key(_urlparse.unquote(str(cwd or "")))
    return not any(label_key(p) in c for p in PROTECTED_WORKSPACE_PATTERNS)


def fs_path_key(path: str | None) -> str:
    r"""Normalize a filesystem path for comparison, preserving raw path literals.

    Decode escapes only in file:// URIs. On Windows, drive paths and UNC
    paths can use extended prefixes, and host-qualified file URIs are UNC
    paths. Interior spaces are part of the path.
    """
    p = str(path or "").strip()
    if p[:7].lower() == "file://":
        p = _urlparse.unquote(p[7:])
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        elif p and p[0] not in "/\\" and (len(p) < 2 or p[1] != ":"):
            p = "//" + p

    if not p:
        return ""

    prefix = p[:8].replace("/", "\\")
    if prefix.lower() == "\\\\?\\unc\\":
        p = "\\\\" + p[8:]
    elif prefix.startswith("\\\\?\\"):
        p = p[4:]

    key = os.path.normcase(os.path.normpath(p))
    drive, tail = os.path.splitdrive(key)
    if drive.startswith("\\\\") and tail == "\\":
        return drive
    return key
