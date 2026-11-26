#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试：service_ensure v2 / card_service v5 / ask_card 自愈 / 候选中文标题补全 / init_hub services.json。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for p in (str(ROOT), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import ask_card  # noqa: E402
import card_service  # noqa: E402
import channel_params  # noqa: E402
import ensure_card_service  # noqa: E402
import init_hub  # noqa: E402
import service_ensure  # noqa: E402


class _MockCardHandler(BaseHTTPRequestHandler):
    health_payload: dict | None = None
    health_status: int = 200
    pending_status: int = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/health"):
            self.send_response(self.health_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = json.dumps(self.health_payload or {}).encode("utf-8")
            self.wfile.write(body)
            return
        if self.path.startswith("/api/pending"):
            self.send_response(self.pending_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]")
            return
        self.send_response(404)
        self.end_headers()


class ServiceEnsureTests(unittest.TestCase):
    def test_card_service_health_endpoint_v5_and_503_when_tk_not_ready(self):
        srv = HTTPServer(("127.0.0.1", 0), card_service.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.dict(os.environ, {"CARD_PORT": str(srv.server_port)}):
                ok, detail = service_ensure.check("card_service", timeout=2.0)
                code, data, _ = service_ensure._http_get_json(
                    f"http://127.0.0.1:{srv.server_port}/api/health",
                    time.monotonic() + 2.0,
                )
            self.assertEqual(code, 200)
            self.assertIsInstance(data, dict)
            self.assertEqual(data.get("service"), "card_service")
            self.assertEqual(data.get("version"), 5)
            self.assertIn("desktop_ok", data)
            self.assertIn("tk_ready", data)
            self.assertIn("tk_heartbeat_age_s", data)
            # 未启动 Tk 弹窗循环时 tk_ready=False，check 必须判不健康且 /api/request 返回 503
            self.assertFalse(ok)
            self.assertTrue(
                detail.startswith(("non_interactive_desktop_instance", "tk_not_ready")),
                msg=f"unexpected detail: {detail}",
            )
            import urllib.request
            import urllib.error
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.server_port}/api/request",
                data=b'{"title":"t","options":[{"id":"1","label":"a"}]}',
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as exc_ctx:
                urllib.request.urlopen(req, timeout=2.0)
            self.assertEqual(exc_ctx.exception.code, 503)
            exc_ctx.exception.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_detects_non_interactive_desktop_and_tk_not_ready(self):
        class FakeDesktopBadHandler(_MockCardHandler):
            health_status = 200
            health_payload = {
                "service": "card_service",
                "ok": False,
                "pid": 12345,
                "session_id": 0,
                "winstation": "Service-0x0",
                "desktop": "Default",
                "desktop_ok": False,
                "tk_ready": True,
            }

        srv = HTTPServer(("127.0.0.1", 0), FakeDesktopBadHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.dict(os.environ, {"CARD_PORT": str(srv.server_port)}), \
                    mock.patch.object(service_ensure, "_run_scheduled_task") as task_mock:
                ok, detail = service_ensure.ensure("card_service", wait=1.0)
            self.assertFalse(ok)
            self.assertIn("non_interactive_desktop_instance", detail)
            task_mock.assert_not_called()
        finally:
            srv.shutdown()
            srv.server_close()

        class FakeTkBadHandler(_MockCardHandler):
            health_status = 200
            health_payload = {
                "service": "card_service",
                "ok": False,
                "pid": 12345,
                "session_id": 1,
                "winstation": "WinSta0",
                "desktop": "Default",
                "desktop_ok": True,
                "tk_ready": False,
                "tk_heartbeat_age_s": 25.0,
                "tk_error": "loop_stalled",
            }

        srv2 = HTTPServer(("127.0.0.1", 0), FakeTkBadHandler)
        t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
        t2.start()
        try:
            with mock.patch.dict(os.environ, {"CARD_PORT": str(srv2.server_port)}):
                ok2, detail2 = service_ensure.check("card_service", timeout=1.5)
            self.assertFalse(ok2)
            self.assertTrue(detail2.startswith("tk_not_ready"))
        finally:
            srv2.shutdown()
            srv2.server_close()

    def test_legacy_pending_fallback_rejected_for_desktop_service(self):
        class LegacyHandler(_MockCardHandler):
            health_status = 404
            pending_status = 200

        srv = HTTPServer(("127.0.0.1", 0), LegacyHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.dict(os.environ, {"CARD_PORT": str(srv.server_port)}):
                ok, detail = service_ensure.check("card_service", timeout=2.0)
                self.assertFalse(ok)
                self.assertEqual(detail, "legacy_instance_no_health")
                self.assertFalse(ensure_card_service.alive(timeout=2.0))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_ensure_triggers_task_and_waits_for_ready(self):
        states = iter([
            (False, "unreachable:down"),
            (False, "unreachable:down"),
            (False, "unreachable:starting"),
            (True, "healthy"),
        ])
        with mock.patch.object(service_ensure, "check", side_effect=lambda *a, **kw: next(states)), \
                mock.patch.object(service_ensure, "_run_scheduled_task", return_value=(True, "triggered")) as trig:
            ok, detail = service_ensure.ensure("card_service", wait=3.0)
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("started_via_task:"))
        trig.assert_called_once()

    def test_ask_card_surfaces_service_ensure_failure(self):
        with mock.patch.object(service_ensure, "ensure", return_value=(False, "计划任务不存在")):
            with self.assertRaises(OSError) as ctx:
                ask_card._request_card(
                    {"title": "t", "groups": []},
                    [{"id": "1", "label": "opt"}],
                    timeout=5.0,
                )
        self.assertIn("选窗卡片服务未生效，尝试自动拉起失败：计划任务不存在", str(ctx.exception))

    def test_dsh_session_title_and_enrich_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            sess_dir = Path(td) / "storages" / "session_projcache" / "sessions"
            sess_dir.mkdir(parents=True)
            (sess_dir / "sid_titled.json").write_text(
                json.dumps({"record": {"rows": {"title": {"val": "验收工作台\n\t"}}}}),
                encoding="utf-8",
            )
            (sess_dir / "sid_prompt.json").write_text(
                json.dumps({
                    "record": {
                        "rows": {
                            "title": {"val": ""},
                            "turnOutline": {"val": {"turns": [{"prompt": "请帮我详细检查一下这轮发布清单、服务自愈状态和回执证据是否完整"}]}},
                        }
                    }
                }),
                encoding="utf-8",
            )
            (sess_dir / "sid_blank.json").write_text(
                json.dumps({"record": {"rows": {}}}),
                encoding="utf-8",
            )
            import session_identity
            with mock.patch.object(session_identity, "dsh_home", return_value=td):
                enriched = channel_params._enrich_candidates([
                    {"sessionId": "sid_titled"},
                    {"sessionId": "sid_prompt"},
                    {"sessionId": "sid_blank"},
                    {"sessionId": "sid_existing", "title": "已有\r\n标题"},
                    {"sessionId": "../escape"},
                ])
        self.assertEqual(enriched[0]["title"], "验收工作台")
        self.assertTrue(enriched[1]["title"].endswith("…"))
        self.assertEqual(enriched[2]["title"], "（空白新对话）")
        self.assertEqual(enriched[3]["title"], "已有 标题")
        self.assertNotIn("title", enriched[4])

    def test_init_hub_installs_services_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hub"
            local_app = Path(td) / "local"
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app)}):
                init_hub.create(root, archive_id=None, force=True)
            svc_file = root / "_state" / "services.json"
            self.assertTrue(svc_file.is_file())
            data = json.loads(svc_file.read_text(encoding="utf-8"))
            self.assertIn("card_service", data.get("services") or {})


if __name__ == "__main__":
    unittest.main()
