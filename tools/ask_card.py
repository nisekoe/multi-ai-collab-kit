"""选窗卡片服务的本机客户端。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
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
    print("卡片已交给宿主服务，等待你选择。", file=sys.stderr, flush=True)
    path = "/api/result?" + urlencode({"id": card["id"]})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = request(path)
        if result.get("status") == "answered":
            choice = result.get("choice")
            if type(choice) is not int or not 0 <= choice < len(flat_options):
                raise ValueError("卡片回答不对应请求里的选项")
            picked = flat_options[choice]
            if picked.get("action") == "new_window":
                return {**picked, "desktop_draft_requested": result.get("desktop_draft_requested") is True}
            return picked
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
    asker: str = "",
    codex_draft_prompt: str = "",
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
    if asker:
        payload["asker"] = asker
    if codex_draft_prompt:
        payload["codex_draft_prompt"] = codex_draft_prompt
    return _request_card(payload, flat, timeout)


def choose(options: list[dict], timeout: float = 300.0) -> str | None:
    """发单选卡片并返回用户所选的窗口标识（兼容旧调用）。"""
    selected = choose_from_groups(
        [{"title": "可投窗口", "options": options}],
        timeout=timeout,
    )
    return selected["id"] if selected is not None else None


def _porta_get(path: str, timeout: float) -> dict:
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:3170{path}", timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Porta 返回了无法识别的结果")
    return result


def _project_titles() -> dict:
    try:
        projects = _porta_get("/api/projects", timeout=5)
    except (OSError, ValueError):
        projects = {}
    titles: dict[str, str] = {}
    for project in (projects.get("projects") or projects.get("items") or []):
        if isinstance(project, dict):
            key = project.get("id") or project.get("projectId")
            name = project.get("name") or project.get("title")
            if key and name:
                titles[str(key)] = str(name)
    for path in (Path.home() / ".gemini" / "config" / "projects").glob("*.json"):
        try:
            project = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(project, dict) and project.get("id") and project.get("name"):
            titles.setdefault(str(project["id"]), str(project["name"]))
    return titles


def porta_groups(limit_each: int = 6, *, binding: str = "") -> list[dict]:
    summaries = _porta_get("/api/conversations", timeout=8).get("trajectorySummaries")
    if not isinstance(summaries, dict):
        raise ValueError("Porta 没有返回对话清单")
    titles = _project_titles()
    outside = "outside-of-project"
    newest = datetime.now(timezone.utc)
    fresh = []
    buckets: dict[str, list[dict]] = {}
    for target, summary in summaries.items():
        if not isinstance(summary, dict):
            continue
        annotations = summary.get("annotations") or {}
        metadata = summary.get("trajectoryMetadata") or {}
        if not isinstance(annotations, dict) or annotations.get("archived"):
            continue
        project = str(metadata.get("projectId") or outside) if isinstance(metadata, dict) else outside
        state = str(summary.get("status") or "").removeprefix("CASCADE_RUN_STATUS_")
        created = str(summary.get("createdTime") or "")
        age = None
        try:
            age = newest - datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
        is_fresh = age is not None and timedelta(0) <= age <= timedelta(minutes=30)
        tags = ["跑着" if state and state != "IDLE" else "空闲"]
        if is_fresh:
            tags.insert(0, "新窗口")
        if binding and target == binding:
            tags.append("当前绑定")
        row = {
            "id": target,
            "label": str(annotations.get("title") or summary.get("summary") or target[:8])[:40],
            "description": ("刚开的 · " if is_fresh else "") + "建 " + created[:16],
            "tags": tags,
            "_last": str(summary.get("lastModifiedTime") or created),
        }
        if is_fresh:
            fresh.append(row)
        buckets.setdefault(project, []).append(row)

    def by_activity(rows):
        return sorted(rows, key=lambda item: item["_last"], reverse=True)

    def options(rows):
        return [{key: value for key, value in row.items() if key != "_last"} for row in rows[:limit_each]]

    fresh = by_activity(fresh)[:limit_each]
    picked_ids = {row["id"] for row in fresh}
    groups = []
    if fresh:
        groups.append({"title": "刚开的窗口（30 分钟内）", "kind": "session", "options": options(fresh)})
    by_project = sorted(buckets, key=lambda project: max(row["_last"] for row in buckets[project]), reverse=True)
    for project in sorted(by_project, key=lambda project: project == outside):
        rows = by_activity(row for row in buckets[project] if row["id"] not in picked_ids)
        if not rows:
            continue
        rows[0]["tags"].insert(0, "最近活跃")
        title = "会话区（不属于项目）" if project == outside else titles.get(project, "未命名项目")
        groups.append({"title": title, "kind": "session" if project == outside else "project", "options": options(rows)})
    return groups


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="向宿主卡片服务请求 Porta 选窗（不发送消息）")
    parser.add_argument("--from-porta", required=True, action="store_true")
    parser.add_argument("--binding", default="")
    parser.add_argument("--title", default="猫猫：选哪个对话？")
    parser.add_argument("--asker", default="assistant")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    try:
        groups = porta_groups(binding=args.binding)
        if not groups:
            print("没有候选，不出卡。", file=sys.stderr)
            return 3
        selected = choose_from_groups(groups, args.timeout, title=args.title, asker=args.asker)
    except (OSError, ValueError) as exc:
        print(f"无法完成选窗：{exc}", file=sys.stderr)
        return 5
    if selected is None:
        return 5
    flat = [item for group in groups for item in group["options"]]
    print(json.dumps({"choice": flat.index(selected), "option": selected["label"], "target": selected["id"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
