"""选窗卡片服务的本机客户端。"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def choose(options: list[dict], timeout: float = 300.0) -> str | None:
    """发单选卡片并返回用户所选的窗口标识。"""
    port = int(os.environ.get("CARD_PORT", "8790"))
    if not 1 <= port <= 65535 or timeout <= 0:
        raise ValueError("CARD_PORT 或选窗等待时间无效")
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def request(path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(base + path, data=data, headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=5) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("卡片服务返回了无法识别的结果")
        return result

    card = request("/api/request", {
        "title": "选择接收消息的 Codex 窗口",
        "groups": [{"title": "可投窗口", "options": options}],
    })
    if not isinstance(card.get("id"), str) or not card["id"]:
        raise ValueError("卡片服务没有返回请求编号")
    print(f"请在卡片或 {base}/ 选择接收窗口。", flush=True)
    path = "/api/result?" + urlencode({"id": card["id"]})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = request(path)
        if result.get("status") == "answered":
            choice = result.get("choice")
            if type(choice) is not int or not 0 <= choice < len(options):
                raise ValueError("卡片回答不对应请求里的选项")
            return options[choice]["id"]
        if result.get("status") != "pending":
            return None
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return None
