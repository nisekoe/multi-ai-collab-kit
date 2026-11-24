"""选窗卡片服务的本机客户端。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import paths  # noqa: E402


def _request_card(payload: dict, flat_options: list[dict], timeout: float) -> dict | None:
    port = paths.card_port()
    if not 1 <= port <= 65535 or timeout <= 0:
        raise ValueError("CARD_PORT 或选窗等待时间无效")
    if not flat_options:
        raise ValueError("卡片选项不能为空")
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def request(path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(base + path, data=data, headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=5) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("卡片服务返回了无法识别的结果")
        return result

    card = request("/api/request", payload)
    if not isinstance(card.get("id"), str) or not card["id"]:
        raise ValueError("卡片服务没有返回请求编号")
    print(f"已在桌面弹出选窗卡片（请勿由 AI 自动拉起浏览器；无桌面环境可手动访问 {base}/）。", flush=True)
    path = "/api/result?" + urlencode({"id": card["id"]})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = request(path)
        if result.get("status") == "answered":
            choice = result.get("choice")
            if type(choice) is not int or not 0 <= choice < len(flat_options):
                raise ValueError("卡片回答不对应请求里的选项")
            return flat_options[choice]
        if result.get("status") != "pending":
            return None
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return None


def choose_from_groups(
    groups: list[dict],
    timeout: float = 300.0,
    *,
    title: str = "选择接收消息的 Codex 窗口",
    subtitle: str = "",
) -> dict | None:
    """发分组单选卡片并返回用户所选的完整选项字典。"""
    normalized: list[dict] = []
    flat: list[dict] = []
    for group in groups:
        opts = [opt for opt in (group.get("options") or []) if isinstance(opt, dict)]
        if not opts:
            continue
        normalized.append({
            "title": group.get("title") or "",
            "kind": group.get("kind") or "",
            "q": group.get("q") or "",
            "options": opts,
        })
        flat.extend(opts)
    payload: dict = {"title": title, "groups": normalized}
    if subtitle:
        payload["subtitle"] = subtitle
    return _request_card(payload, flat, timeout)


def choose(options: list[dict], timeout: float = 300.0) -> str | None:
    """发单选卡片并返回用户所选的窗口标识（兼容旧调用）。"""
    selected = choose_from_groups(
        [{"title": "可投窗口", "options": options}],
        timeout=timeout,
    )
    return selected["id"] if selected is not None else None
