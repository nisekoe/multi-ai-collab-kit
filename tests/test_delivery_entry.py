"""Exercise the shipping entry points against fake local state and transports."""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testenv  # makes the tools directory available
import msg_envelope
import porta_adapter
import reach_gpt
import receipt_tri
import send_codex

FIRST = "aaaaaaaa-0000-4000-8000-000000000001"
SECOND = "bbbbbbbb-0000-4000-8000-000000000002"
THIRD = "cccccccc-0000-4000-8000-000000000003"
SENDER = "harbor|local|fixture-sender"
SENTINEL = Path(__file__).resolve().parents[1] / "templates" / ".archive-root.json"


class CardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(value).encode("utf-8"))

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.groups = data.get("groups") or []
        self.server.ids = [row["id"] for g in self.server.groups for row in g.get("options", [])]
        self.reply({"id": "fixture-card"})

    def do_GET(self):
        choice = getattr(self.server, "answer_choice", 1)
        payload = {"status": "answered", "choice": choice}
        if hasattr(self.server, "desktop_draft_requested"):
            payload["desktop_draft_requested"] = self.server.desktop_draft_requested
        self.reply(payload)


class DeliveryEntryTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.home = root / "codex"
        (self.home / "sessions").mkdir(parents=True)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.hub = root / "hub"
        self.hub.mkdir()
        (self.hub / ".archive-root.json").write_bytes(SENTINEL.read_bytes())
        stack.enter_context(mock.patch.dict(os.environ, {
            "ARCHIVE_ROOT": str(self.hub), "ARCHIVE_ID_EXPECTED": "user-multiagent-hub",
            "LOCALAPPDATA": str(root / "profile"),
        }))
        self.history = root / "receipts.jsonl"
        self.result = root / "machine.json"
        stack.enter_context(mock.patch.object(receipt_tri, "HISTORY", self.history))
        stack.enter_context(mock.patch.object(reach_gpt, "CODEX_HOME", str(self.home)))
        stack.enter_context(mock.patch.object(reach_gpt, "REG", str(root / "absent-registry.json")))
        stack.enter_context(mock.patch.object(reach_gpt, "RECEIPT", str(root / "legacy-receipt.json")))
        stack.enter_context(mock.patch.object(reach_gpt, "find_codex", return_value="fake-codex"))
        self.transport = stack.enter_context(mock.patch.object(reach_gpt.subprocess, "run"))
        self.transport.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        self.state_db = self.home / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(self.state_db)) as db, db:
            db.execute("""CREATE TABLE threads (
                id, name, title, cwd, source, archived, updated_at, rollout_path
            )""")
        self.make_thread(FIRST, "Alpha")
        self.make_thread(SECOND, "Beta")

    def make_thread(self, identity, title):
        path = self.home / "sessions" / f"rollout-fixture-{identity}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n", encoding="utf-8")
        os.utime(path, (time.time() - 180, time.time() - 180))
        with contextlib.closing(sqlite3.connect(self.state_db)) as db, db:
            db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, 'vscode', 0, ?, ?)",
                       (identity, title, title, str(self.workspace), time.time() - 180, str(path)))
        return path

    def invoke(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = send_codex.main([*args, "--text", "synthetic report", "--from-addr", SENDER,
                                    "--result-file", str(self.result)])
        return code, stream.getvalue()

    def last_receipt(self):
        return json.loads(self.history.read_text(encoding="utf-8").splitlines()[-1])

    def finish_message(self, command, **kwargs):
        if command[1] != "exec":
            raise AssertionError(command)
        identity, body = command[3:5]
        path = self.home / "sessions" / f"rollout-fixture-{identity}.jsonl"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with path.open("a", encoding="utf-8") as stream:
            for kind, payload in (
                ("response_item", {"type": "message", "role": "user", "content": [{"text": body}]}),
                ("event_msg", {"type": "task_complete", "last_agent_message": "received"}),
            ):
                stream.write(json.dumps({"type": kind, "timestamp": stamp, "payload": payload}) + "\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_card_choice_reaches_transport_and_can_be_reused(self):
        self.transport.side_effect = self.finish_message
        with ThreadingHTTPServer(("127.0.0.1", 0), CardHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with mock.patch.dict(os.environ, {"CARD_PORT": str(server.server_port)}):
                    code, output = self.invoke("--ask", "--new-cwd", str(self.workspace),
                                               "--task", "fixture", "--mode", "sync", "--card-timeout", "3")
                self.assertEqual(server.ids, [FIRST, SECOND, f"{send_codex.NEW_OPTION_PREFIX}{self.workspace}"])
            finally:
                server.shutdown()
                worker.join(timeout=3)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.last_receipt()["state"], "consumed")
        command = self.transport.call_args.args[0]
        self.assertEqual(command[3], SECOND)
        self.assertEqual(msg_envelope.parse(command[4])["from"], SENDER)
        self.assertEqual(msg_envelope.parse(command[4])["to"], f"codex|local|{SECOND}")
        rollout = self.home / "sessions" / f"rollout-fixture-{SECOND}.jsonl"
        os.utime(rollout, (time.time() - 180, time.time() - 180))
        code, output = self.invoke("--task", "fixture", "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"codex|local|{SECOND}", output)
        self.make_thread(THIRD, "New window")
        code, output = self.invoke("--task", "fixture", "--dry-run")
        self.assertEqual(code, 4, output)
        self.assertIn("同一工作区出现新候选", output)

    def test_interrupted_queue_is_blocked_and_sync_is_explicit(self):
        with contextlib.closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_turns (thread_id, status, rollout_ordinal)")
            db.execute("INSERT INTO thread_turns VALUES (?, 'interrupted', 1)", (FIRST,))
        code, output = self.invoke("--reply-to", f"codex|local|{FIRST}")
        self.assertEqual(code, 4, output)
        self.assertIn("上一轮被中断", output)
        self.assertEqual(self.last_receipt()["state"], "refused")
        self.assertEqual(json.loads(self.result.read_text(encoding="utf-8"))["reason"], "last_turn_interrupted")
        self.transport.assert_not_called()
        self.transport.side_effect = self.finish_message
        code, output = self.invoke("--reply-to", f"codex|local|{FIRST}", "--mode", "sync")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.last_receipt()["state"], "consumed")

    def test_grouped_card_has_new_window_option_in_every_group(self):
        proj_dir = self.workspace / "proj-alpha"
        proj_dir.mkdir()
        with contextlib.closing(sqlite3.connect(self.state_db)) as db, db:
            db.execute("CREATE TABLE projects (id, name, metadata)")
            db.execute("CREATE TABLE project_roots (project_id, path)")
            db.execute("INSERT INTO projects VALUES ('p1', 'AlphaProj', '{}')")
            db.execute("INSERT INTO project_roots VALUES ('p1', ?)", (str(proj_dir),))
            db.execute("UPDATE threads SET cwd = ? WHERE id = ?", (str(proj_dir), FIRST))
        snap = reach_gpt.read_snapshot()
        groups = send_codex._candidate_groups(snap, default_new_cwd=str(self.workspace), include_new=True)
        self.assertEqual([g["kind"] for g in groups], ["project", "session"])
        self.assertEqual(groups[0]["title"], "项目 · AlphaProj")
        self.assertEqual(groups[0]["options"][-1]["action"], "new_window")
        self.assertEqual(groups[0]["options"][-1]["cwd"], str(proj_dir))
        self.assertEqual(groups[1]["title"], "会话区（无项目分类）")
        self.assertEqual(groups[1]["options"][-1]["action"], "new_window")
        self.assertEqual(groups[1]["options"][-1]["cwd"], str(self.workspace))

    def test_card_pick_new_window_opens_desktop_draft_and_explicit_new_registers(self):
        # 1. 卡片点「新建」：宿主未确认打开草稿时拒绝回退后台
        with ThreadingHTTPServer(("127.0.0.1", 0), CardHandler) as server:
            server.answer_choice = 2
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with mock.patch.dict(os.environ, {"CARD_PORT": str(server.server_port)}):
                    code, output = self.invoke("--ask", "--new-cwd", str(self.workspace),
                                               "--task", "new-win-task", "--card-timeout", "3")
            finally:
                server.shutdown()
                worker.join(timeout=3)
        self.assertEqual(code, 5, output)
        self.assertIn("desktop_draft_not_confirmed", output)
        self.transport.assert_not_called()

        # 2. 卡片点「新建」：宿主确认打开前台草稿时返回 0，不悄悄跑后台 exec
        with ThreadingHTTPServer(("127.0.0.1", 0), CardHandler) as server:
            server.answer_choice = 2
            server.desktop_draft_requested = True
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with mock.patch.dict(os.environ, {"CARD_PORT": str(server.server_port)}):
                    code, output = self.invoke("--ask", "--new-cwd", str(self.workspace),
                                               "--task", "new-win-task", "--card-timeout", "3")
            finally:
                server.shutdown()
                worker.join(timeout=3)
        self.assertEqual(code, 0, output)
        self.assertIn("desktop_draft_requested", output)
        self.transport.assert_not_called()

        # 3. 显式 --new：走后台 codex exec (stdin 传正文)，登记 created_by_kit 并支持 --task 复用
        created_id = "dddddddd-0000-4000-8000-000000000004"

        def fake_exec_new(command, **kwargs):
            self.assertEqual(command[1:4], ["exec", "--json", "-C"])
            self.assertEqual(command[4], str(self.workspace))
            self.assertEqual(command[5:7], ["--skip-git-repo-check", "-"])
            body = kwargs.get("input") or ""
            self.assertEqual(msg_envelope.parse(body)["from"], SENDER)
            path = self.home / "sessions" / f"rollout-fixture-{created_id}.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": created_id}}) + "\n", encoding="utf-8")
            os.utime(path, (time.time() - 180, time.time() - 180))
            with contextlib.closing(sqlite3.connect(self.state_db)) as db, db:
                db.execute("INSERT INTO threads VALUES (?, 'NewExec', 'NewExec', ?, 'exec', 0, ?, ?)",
                           (created_id, str(self.workspace), time.time() - 180, str(path)))
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": created_id}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hello from new"}}),
                json.dumps({"type": "turn.completed"}),
            ]) + "\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        self.transport.side_effect = fake_exec_new
        code, output = self.invoke("--new", "--new-cwd", str(self.workspace), "--task", "new-win-task")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.last_receipt()["state"], "consumed")
        self.assertEqual(self.last_receipt()["target"], created_id)
        reg_data = json.loads(Path(reach_gpt.REG).read_text(encoding="utf-8"))
        self.assertTrue(any(r.get("thread_id") == created_id and r.get("created_by_kit") for r in reg_data["lines"]))
        code, output = self.invoke("--task", "new-win-task", "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"codex|local|{created_id}", output)

    def test_new_window_blocks_protected_workspace_and_supports_dry_run(self):
        protected = self.workspace / "protected-workspace"
        protected.mkdir()
        code, output = self.invoke("--new", "--new-cwd", str(protected))
        self.assertEqual(code, 6, output)
        self.assertIn("workspace_protected", output)
        self.assertEqual(self.last_receipt()["state"], "refused")
        self.transport.assert_not_called()

        code, output = self.invoke("--new", "--new-cwd", str(self.workspace), "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("[dry-run]", output)
        self.transport.assert_not_called()

    def test_card_port_three_tier_priority(self):
        import paths
        ch = self.hub / "_state" / "通道.json"
        ch.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CARD_PORT", None)
            os.environ.pop("DSH_CHANNEL_FILE", None)
            self.assertEqual(paths.card_port(), 8790)
            ch.write_text(json.dumps({"card_port": 18799}), encoding="utf-8")
            self.assertEqual(paths.card_port(), 18799)
            os.environ["CARD_PORT"] = "18888"
            self.assertEqual(paths.card_port(), 18888)

    def test_queue_acceptance_and_transport_timeout_are_distinct(self):
        with mock.patch.object(reach_gpt, "QUEUE_WAIT", 0):
            code, output = self.invoke("--thread", FIRST, "--confirmed")
        self.assertEqual(code, 5, output)
        self.assertEqual(self.last_receipt()["state"], "queued")
        self.transport.side_effect = subprocess.TimeoutExpired("fake-codex", 1)
        code, output = self.invoke("--thread", FIRST, "--confirmed", "--mode", "sync")
        self.assertEqual(code, 5, output)
        self.assertEqual(self.last_receipt()["state"], "unknown")
        state = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertIsNone(state["status"])
        self.assertFalse(state["resultKnown"])

    def test_wrong_leg_and_direct_transport_are_rejected(self):
        self.assertEqual(self.invoke("--reply-to", f"porta|local|{FIRST}")[0], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reach_gpt.main(["--thread", FIRST, "--text", "direct"]), 4)
        self.transport.assert_not_called()


class PortaReceiptTest(unittest.TestCase):
    def test_binding_matches_dry_run_and_send_with_return_address(self):
        def candidate(identity, created):
            return SimpleNamespace(id=identity, hostId="local", cwd="fixture", title="Fixture",
                                   role="peer", reachable=True, truncated=False, lastActivity="", status="idle",
                                   meta={"stepCount": 1, "createdTime": created})

        older = candidate(FIRST, "2026-01-01T00:00:00Z")
        newer = candidate(SECOND, "2026-01-02T00:00:00Z")
        transport = mock.Mock()
        transport.post_message.return_value = (200, {})
        adapter = porta_adapter.PortaAdapter(client=transport)
        adapter.enumerate = mock.Mock(return_value=SimpleNamespace(candidates=[older, newer], ok=True))
        adapter.verify_target = mock.Mock(return_value=SimpleNamespace(ok=True, candidate=newer))
        adapter._inspect_delivery_steps = mock.Mock(return_value={
            "stepCount": 2, "matchedBodyFingerprint": True, "matchedClientMessageId": True,
        })
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(receipt_tri, "HISTORY", Path(temp) / "receipts.jsonl"), \
                contextlib.redirect_stdout(io.StringIO()):
            for dry_run in (True, False):
                code, result = adapter.deliver(FIRST, "test", peer_agent="fixture",
                                               binding=FIRST, dry_run=dry_run)
                self.assertEqual(code, 6)
                self.assertEqual(result["reason"], "new_window_needs_confirm")
            transport.post_message.assert_not_called()
            code, result = adapter.deliver(SECOND, "test", peer_agent="fixture", binding=SECOND,
                                           from_addr=SENDER, wait_for_reply=False)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "consumed")
        self.assertTrue(result["verified"])
        self.assertEqual(msg_envelope.parse(transport.post_message.call_args.args[1])["from"], SENDER)

    def test_receipts_use_transport_and_message_evidence(self):
        cases = (
            (0, {"http": 200, "matchedBodyFingerprint": True}, "consumed"),
            (5, {"stage": "post_verify", "http": 200}, "queued"),
            (6, {"stage": "binding_check", "reason": "new_window_needs_confirm"}, "refused"),
            (5, {"stage": "post_message", "reason": "http_error", "http": 503}, None),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(receipt_tri, "HISTORY", Path(tmp) / "receipts.jsonl"):
                adapter = porta_adapter.PortaAdapter()
                for code, info, expected in cases:
                    with self.subTest(expected=expected):
                        with mock.patch.object(adapter, "_deliver_impl", return_value=(code, info)), \
                                contextlib.redirect_stdout(io.StringIO()):
                            _, result = adapter.deliver(FIRST, "fixture")
                        self.assertEqual(result["status"], expected)
                        self.assertEqual(result["resultKnown"], expected is not None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
