#!/usr/bin/env python3
"""Codex 投递入口。先选择完整地址，再在发送层复验目标与回合。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import UUID

import ask_card
import codex_picker_adapter as codex
import msg_envelope
import paths
import reach_gpt
import receipt_tri


def _thread_id(value: str) -> str:
    if not value:
        raise ValueError("请提供完整的会话地址")
    return str(UUID(value))


def _task_path(task: str) -> Path:
    digest = hashlib.sha256(task.encode("utf-8")).hexdigest()
    return paths.resolve(f"_state/window_registry/task-choices/{digest}.json", for_write=True)


def _save_choice(path: Path, thread_id: str, cwd: str, neighbors: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"thread": thread_id, "cwd": cwd, "neighbors": sorted(neighbors)}
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        temporary = Path(temp) / path.name
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


def _candidate_options(snapshot):
    options = []
    for key, row in snapshot.threads.items():
        if codex.verify_target(snapshot, key, workspace_allowed=codex.default_workspace_allowed).reason:
            continue
        label = snapshot.bindings.get(key, {}).get("alias") or row.get("title") or row.get("name") or "无标题"
        options.append({"id": key[2], "label": label, "description": row.get("cwd") or ""})
    return sorted(options, key=lambda option: (option["label"], option["id"]))


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="选择目标并投递到 Codex")
    parser.add_argument("--thread", default="")
    parser.add_argument("--task", default="", help="保存或复用此任务已确认的窗口")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--ask", action="store_true", help="用本机卡片服务选择窗口")
    selection.add_argument("--reply-to", default="", help="来件里的发件地址 codex|local|会话号")
    selection.add_argument("--confirmed", action="store_true", help="用户已点名 --thread 对应的窗口")
    parser.add_argument("--card-timeout", type=float, default=300, help="等待选窗的秒数")
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--file")
    body.add_argument("--text")
    parser.add_argument("--from-addr", default="", help="本轮发件地址，默认为当前宿主会话")
    parser.add_argument("--mode", choices=("sync", "queue"), default="queue")
    parser.add_argument("--registry")
    parser.add_argument("--expected-cwd")
    parser.add_argument("--nonce", default="")
    parser.add_argument("--result-file")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    thread_id = ""

    def reject(reason: str, detail: str, code: int = 4, *, known: bool = True) -> int:
        print(detail)
        state = {"status": None, "resultKnown": False}
        if not args.dry_run:
            state = receipt_tri.report("codex", thread_id, error=reason if known else None, reason=reason)
        if args.result_file:
            result = {**state, "nonce": args.nonce, "thread": thread_id, "mode": args.mode,
                      "exit": code, "reason": reason}
            try:
                Path(args.result_file).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                print(f"无法写入机器结果：{exc}", file=sys.stderr)
        return code

    try:
        if args.thread:
            thread_id = _thread_id(args.thread)
        if args.reply_to:
            route = msg_envelope.split_addr(args.reply_to)
            if route.get("leg") != "codex" or route.get("hostId") != reach_gpt.LOCAL_HOST_ID:
                raise ValueError("--reply-to 必须指向本机 Codex")
            returned = _thread_id(route.get("sessionId", ""))
            if thread_id and thread_id != returned:
                raise ValueError("--thread 与回程地址指向不同窗口")
            thread_id = returned
        elif args.confirmed and not thread_id:
            raise ValueError("--confirmed 需要 --thread")
        elif args.ask and thread_id:
            raise ValueError("使用 --ask 时由卡片选择目标")
        elif not (args.ask or args.confirmed or args.task):
            return reject("selection_required", "请选择 --ask、--reply-to、--task，或由用户点名后加 --confirmed。")
    except ValueError as exc:
        return reject("invalid_address", str(exc), 2)

    binding = None
    if args.task and not args.reply_to:
        try:
            binding = _task_path(args.task)
        except paths.RootError as exc:
            return reject("no_bound_archive", str(exc), 2)
    try:
        snapshot = reach_gpt.read_snapshot(args.registry)
        if args.ask:
            options = _candidate_options(snapshot)
            if not options:
                return reject("no_eligible_windows", "当前没有可选的本机 Codex 窗口。")
            picked = ask_card.choose(options, args.card_timeout)
            if picked is None:
                return reject("choice_incomplete", "选窗已取消或超时。", known=False)
            thread_id = _thread_id(picked)
        elif not (args.confirmed or args.reply_to):
            try:
                saved = json.loads(binding.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return reject("task_choice_missing", "此任务还没有选窗记录，请用 --ask 或让用户明确点名。")
            selected = _thread_id(saved.get("thread", ""))
            if thread_id and selected != thread_id:
                return reject("task_choice_mismatch", "指定窗口与任务的选窗记录不同，请重新确认。")
            thread_id = selected

        key = codex.address(reach_gpt.LOCAL_HOST_ID, thread_id)
        row = snapshot.threads.get(key, {})
        cwd = row.get("cwd") or ""
        expected_cwd = args.expected_cwd if args.expected_cwd is not None else row.get("cwd")
        if binding and not (args.ask or args.confirmed):
            expected_cwd = saved["cwd"]
            if args.expected_cwd is not None and codex.fs_path_key(args.expected_cwd) != codex.fs_path_key(expected_cwd):
                return reject("cwd_drift", "期望工作区与选窗记录不符。", 6)
            if row and codex.fs_path_key(cwd) == codex.fs_path_key(expected_cwd):
                in_workspace = _neighbors(snapshot, cwd)
                if in_workspace.difference(saved["neighbors"]):
                    return reject("new_window_needs_confirm", "同一工作区出现新候选，请用 --ask 重新确认窗口。")
    except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
        return reject("selection_unavailable", f"无法取得选窗结果：{exc}", 2, known=False)

    command = ["--thread", thread_id, "--mode", args.mode]
    command.extend(("--file", args.file) if args.file else ("--text", args.text))
    for name, value in (("--registry", args.registry), ("--expected-cwd", expected_cwd),
                        ("--nonce", args.nonce), ("--result-file", args.result_file)):
        if value is not None:
            command.extend((name, value))
    if args.dry_run:
        command.append("--dry-run")
    from_addr = args.from_addr or os.environ.get("KIT_FROM_ADDR", "")
    if not from_addr:
        if os.environ.get("DSH_SESSION_ID"):
            from_addr = msg_envelope.addr("harbor", "local", os.environ["DSH_SESSION_ID"])
        elif os.environ.get("CODEX_THREAD_ID"):
            from_addr = msg_envelope.addr("codex", "local", os.environ["CODEX_THREAD_ID"])
    code = reach_gpt.main(command, selected=True, confirmed=bool(args.confirmed or args.ask),
                          from_addr=from_addr, task_id=args.task)

    if binding and not args.dry_run and (args.ask or args.confirmed) and row and code in (0, 5):
        try:
            _save_choice(binding, thread_id, cwd, _neighbors(snapshot, cwd))
        except OSError as exc:
            print(f"本次回执已生成，保存选窗记录时出错：{exc}", file=sys.stderr)
    return code


def _neighbors(snapshot, cwd: str) -> set[str]:
    path = codex.fs_path_key(cwd)
    return {option["id"] for option in _candidate_options(snapshot)
            if codex.fs_path_key(option["description"]) == path}


if __name__ == "__main__":
    raise SystemExit(main())
