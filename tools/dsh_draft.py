"""Create a new DeepSeek Harness (DSH) session via Harbor /api/harbor/new-session, with dsh://open fallback."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DSH_OPEN_URL = "dsh://open"
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def open_link() -> str:
    return DSH_OPEN_URL


def request_open() -> None:
    if os.name != "nt":
        raise OSError("唤起 DSH 桌面端需要 Windows 桌面宿主")
    os.startfile(open_link())


def _try_open() -> bool:
    try:
        request_open()
        return True
    except OSError:
        return False


def request_new_session(
    base_url: str,
    token: str,
    *,
    cwd: str = "",
    agent_preset: str = "",
    timeout: float = 10.0,
) -> dict:
    """Call POST /api/harbor/new-session (bypassing system proxy); fall back to dsh://open if unavailable."""
    payload: dict = {}
    if cwd:
        payload["cwd"] = cwd
    if agent_preset:
        payload["agentPreset"] = agent_preset
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = base_url.rstrip("/") + "/api/harbor/new-session"
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-Harbor-Token": token},
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(body, dict) and body.get("ok") and body.get("sessionId"):
            opened = _try_open()
            return {
                "ok": True,
                "created": True,
                "sessionId": str(body["sessionId"]),
                "agentPreset": body.get("agentPreset"),
                "opened": opened,
            }
        err = (body or {}).get("error") if isinstance(body, dict) else "invalid_response"
        opened = _try_open()
        return {"ok": False, "created": False, "opened": opened, "error": str(err or "failed")}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        opened = _try_open()
        return {"ok": False, "created": False, "opened": opened, "error": f"{type(exc).__name__}: {exc}"}


def request_new_session_from_channel(
    *,
    cwd: str = "",
    agent_preset: str = "",
    timeout: float = 10.0,
) -> dict:
    """Read Harbor endpoint & token on demand from channel_params (never persisted in card JSON)."""
    try:
        import channel_params as CP

        getter = getattr(CP, "momo", None) or getattr(CP, "desktop_bot", None)
        if getter is None:
            raise RuntimeError("channel_params has no desktop side getter")
        side = getter()
    except Exception as exc:  # noqa: BLE001
        opened = _try_open()
        return {"ok": False, "created": False, "opened": opened, "error": f"{type(exc).__name__}: {exc}"}
    return request_new_session(
        str(side.get("base") or ""),
        str(side.get("token") or ""),
        cwd=cwd,
        agent_preset=agent_preset,
        timeout=timeout,
    )
