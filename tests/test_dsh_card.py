"""Unit tests for channel_params proxy bypass, Harbor /new-session, DSH card open/create, and reach_dsh --ask."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import _testenv  # noqa: F401
_testenv.sandbox()
import card_service
import channel_params as CP
import dsh_draft
import reach_dsh


class _EchoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"ok": True, "path": self.path, "token": self.headers.get("X-Harbor-Token")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/harbor/new-session":
            if self.headers.get("X-Harbor-Token") != "tok-1":
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"unauthorized"}')
                return
            body = json.dumps({
                "ok": True,
                "created": True,
                "sessionId": "session-created-123456",
                "agentPreset": data.get("agentPreset") or "default",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"ok": True, "echo": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class _ScriptedCardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        self.server.requests.append(data)
        idx = len(self.server.requests) - 1
        self._reply({"id": f"card-{idx}"})

    def do_GET(self):
        if self.path.startswith("/api/health"):
            return self._reply({"service": "card_service", "ok": True, "desktop_ok": True, "tk_ready": True})
        qid = self.path.split("id=", 1)[-1] if "id=" in self.path else "card-0"
        idx = int(qid.split("-")[-1])
        ans = self.server.answers[idx]
        self._reply(ans)


class DshCardAndProxyTest(unittest.TestCase):
    def test_channel_params_and_dsh_draft_bypass_system_proxy(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with mock.patch.dict(os.environ, {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                }):
                    st1, b1 = CP._get(base + "/api/harbor/health", "tok-1", timeout=3.0)
                    st2, b2 = CP.post_json(base + "/api/harbor/say", "tok-1", {"text": "hi"}, timeout=3.0)
                    with mock.patch.object(dsh_draft, "request_open") as open_mock:
                        created = dsh_draft.request_new_session(base, "tok-1", timeout=3.0)
                self.assertEqual(st1, 200)
                self.assertEqual(b1["token"], "tok-1")
                self.assertEqual(st2, 200)
                self.assertEqual(b2["echo"], {"text": "hi"})
                self.assertTrue(created["ok"])
                self.assertTrue(created["created"])
                self.assertEqual(created["sessionId"], "session-created-123456")
                open_mock.assert_called_once_with()
            finally:
                server.shutdown()
                worker.join(timeout=3)

    def test_dsh_draft_falls_back_to_open_when_harbor_unreachable(self):
        with mock.patch.object(dsh_draft, "request_open") as open_mock:
            res = dsh_draft.request_new_session("http://127.0.0.1:1", "tok-1", timeout=1.0)
        self.assertFalse(res["created"])
        self.assertTrue(res["opened"])
        open_mock.assert_called_once_with()

    def test_card_service_desktop_answer_supports_dsh_create_fallback_and_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            card_dir = Path(tmp)
            with mock.patch.object(card_service, "CARD_DIR", card_dir):
                # 1. DSH auto-create succeeds
                rec_dsh_created = {
                    "id": "c_dsh_created",
                    "status": "pending",
                    "flat": [{"id": "__new__:dsh", "action": "new_window", "app": "dsh"}],
                }
                card_service._write(rec_dsh_created)
                with mock.patch.object(
                    dsh_draft,
                    "request_new_session_from_channel",
                    return_value={"ok": True, "created": True, "sessionId": "session-auto-9999", "opened": True},
                ) as create_mock, mock.patch.object(card_service.codex_draft, "request_draft") as codex_mock:
                    card_service._answer_on_desktop(rec_dsh_created, 0, "")
                create_mock.assert_called_once()
                codex_mock.assert_not_called()
                saved = card_service._read("c_dsh_created")
                self.assertTrue(saved["desktop_draft_requested"])
                self.assertEqual(saved["created_session_id"], "session-auto-9999")

                # 2. DSH falls back to foreground open only
                rec_dsh_open = {
                    "id": "c_dsh_open",
                    "status": "pending",
                    "flat": [{"id": "__new__:dsh", "action": "new_window", "app": "dsh"}],
                }
                card_service._write(rec_dsh_open)
                with mock.patch.object(
                    dsh_draft,
                    "request_new_session_from_channel",
                    return_value={"ok": False, "created": False, "opened": True, "error": "refused"},
                ):
                    card_service._answer_on_desktop(rec_dsh_open, 0, "")
                saved_open = card_service._read("c_dsh_open")
                self.assertTrue(saved_open["desktop_draft_requested"])
                self.assertNotIn("created_session_id", saved_open)

                # 3. Codex branch unchanged
                rec_codex = {
                    "id": "c_codex_ok",
                    "status": "pending",
                    "flat": [{"id": "__new__:C:/work", "action": "new_window", "cwd": "C:/work"}],
                }
                card_service._write(rec_codex)
                with mock.patch.object(card_service.codex_draft, "request_draft") as codex_mock, \
                        mock.patch.object(dsh_draft, "request_new_session_from_channel") as dsh_mock:
                    card_service._answer_on_desktop(rec_codex, 0, "hello prompt")
                codex_mock.assert_called_once_with("C:/work", "hello prompt")
                dsh_mock.assert_not_called()
                self.assertTrue(card_service._read("c_codex_ok")["desktop_draft_requested"])

    def test_reach_dsh_ask_one_click_create_delivers_directly(self):
        fake_side = {"base": "http://127.0.0.1:9999", "token": "tok", "session_cwd": "default-workspace"}
        initial_err = {"ok": False, "error": "binding_invalid", "verify_error": "target_archived",
                       "needs_confirm": True, "candidates": []}
        created_ok = {"ok": True, "sessionId": "session-auto-9999", "role": "desktop",
                      "source": "live-header", "basis": ["explicit_session_id"]}

        with ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedCardHandler) as server:
            server.requests = []
            # Single click: card service created session-auto-9999 directly
            server.answers = [
                {"status": "answered", "choice": 0, "desktop_draft_requested": True,
                 "created_session_id": "session-auto-9999"},
            ]
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                out = io.StringIO()
                with mock.patch.dict(os.environ, {"CARD_PORT": str(server.server_port)}), \
                        mock.patch.object(CP, "desktop_bot", return_value=fake_side), \
                        mock.patch.object(CP, "refresh_identity", return_value={"ok": True}), \
                        mock.patch.object(CP, "resolve_desktop_session", side_effect=[initial_err, created_ok]) as res_mock, \
                        mock.patch.object(CP, "post_json_soft", return_value=(200, {"ok": True, "accepted": True, "mode": "steer", "role": "desktop"})), \
                        mock.patch.object(reach_dsh, "record", return_value=True), \
                        contextlib.redirect_stdout(out):
                    code = reach_dsh.main(["--ask", "--card-timeout", "3", "--text", "hello auto new"])
                self.assertEqual(code, 0, out.getvalue())
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(res_mock.call_args_list[1].kwargs.get("explicit"), "session-auto-9999")
                self.assertIn("已受理", out.getvalue())
            finally:
                server.shutdown()
                worker.join(timeout=3)

    def test_reach_dsh_ask_open_then_rescan_delivers(self):
        fake_side = {"base": "http://127.0.0.1:9999", "token": "tok", "session_cwd": "default-workspace"}
        initial_err = {"ok": False, "error": "binding_invalid", "verify_error": "target_archived",
                       "needs_confirm": True, "candidates": []}
        rescan_ok = {"ok": True, "sessionId": "session-new-1234567890", "role": "desktop",
                     "source": "live-header", "basis": ["unique_candidate"]}

        with ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedCardHandler) as server:
            server.requests = []
            server.answers = [
                {"status": "answered", "choice": 0, "desktop_draft_requested": True},
                {"status": "answered", "choice": 1},
            ]
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                out = io.StringIO()
                with mock.patch.dict(os.environ, {"CARD_PORT": str(server.server_port)}), \
                        mock.patch.object(CP, "desktop_bot", return_value=fake_side), \
                        mock.patch.object(CP, "refresh_identity", return_value={"ok": True}), \
                        mock.patch.object(CP, "resolve_desktop_session", side_effect=[initial_err, rescan_ok]), \
                        mock.patch.object(CP, "post_json_soft", return_value=(200, {"ok": True, "accepted": True, "mode": "steer", "role": "desktop"})), \
                        mock.patch.object(reach_dsh, "record", return_value=True), \
                        contextlib.redirect_stdout(out):
                    code = reach_dsh.main(["--ask", "--card-timeout", "3", "--text", "hello dsh"])
                self.assertEqual(code, 0, out.getvalue())
                self.assertEqual(len(server.requests), 2)
                self.assertIn("dsh://open", out.getvalue())
                self.assertIn("已受理", out.getvalue())
            finally:
                server.shutdown()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
