"""Fixture HTTP tests: no production endpoints, models or hardware."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from server.contracts import project_status
from server.main import PlatformServer, parse_origin, safe_target, StatusCache, UpstreamError

BOOT = "01234567-89ab-cdef-0123-456789abcdef"


def fixture_state():
    return {
        "connected": True, "csrf_token": "SECRET-CSRF",
        "config": {"password": "SECRET-CONFIG"},
        "capabilities": {"v2_deploy": {"available": False, "reason": "model missing SECRET-ERROR"}},
        "jobs": [{"id": "old-failure", "action": "v2_deploy", "state": "failed", "error": "SECRET-ERROR"}],
        "hub": {"api_reachable": True, "recording": {"active": False, "saving": False}},
        "usb": {"verified": True},
        "usb_storage": {"selected": {"uuid": "TEST-USB", "mountpoint": "/media/test/USB", "available_bytes": 1000}},
        "precision_workbench": {
            "fresh": True, "models": {
                "FP32": {"status": "STOPPED", "ready": False, "busy": False, "lease_active": False},
                "BF16": {"status": "STOPPED", "ready": False, "busy": False, "lease_active": False}},
            "robot_profile": {"ready": True, "boot_id": BOOT},
            "v2_profile": {"ready": True, "fresh": True, "acceptance": {"validated": False}}}}


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.calls.append(("GET", self.path, dict(self.headers), b""))
        if self.server.mode == "disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if self.server.mode == "slow":
            time.sleep(0.35)
        if self.server.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/escape")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/state":
            raw = json.dumps(self.server.state).encode()
            content_type = "application/json" if self.server.mode != "html" else "text/html"
        else:
            raw, content_type = ("legacy:" + self.path).encode(), "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.calls.append(("POST", self.path, dict(self.headers), raw))
        if self.server.mode == "disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        body = b'{"job":{"id":"fixture-job","state":"queued"}}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.web = Path(cls.tmp.name) / "web"
        cls.web.mkdir()
        (cls.web / "index.html").write_text("new-management")
        (cls.web / "platform.mjs").write_text("export const fixture = true;")
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.upstream.daemon_threads = True
        cls.upstream.mode, cls.upstream.calls, cls.upstream.state = "ok", [], fixture_state()
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()
        cls.facade = PlatformServer(0, f"http://127.0.0.1:{cls.upstream.server_port}", cls.web,
                                   timeout=0.15, cache_ttl=0)
        cls.facade_thread = threading.Thread(target=cls.facade.serve_forever, daemon=True)
        cls.facade_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.facade.shutdown()
        cls.facade.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.upstream.mode, self.upstream.calls, self.upstream.state = "ok", [], fixture_state()

    def request(self, method, target, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.facade.server_port, timeout=2)
        values = {"Host": f"127.0.0.1:{self.facade.server_port}"}
        values.update(headers or {})
        conn.request(method, target, body, values)
        response = conn.getresponse()
        result = response.status, dict(response.headers), response.read()
        conn.close()
        return result

    def post_headers(self):
        return {"Origin": f"http://127.0.0.1:{self.facade.server_port}",
                "X-Tiger-Token": "fixture-token", "Content-Type": "application/json"}

    def status(self):
        code, _, raw = self.request("GET", "/platform/status")
        self.assertEqual(code, 200)
        return json.loads(raw)

    def test_health_is_lightweight_and_never_reads_legacy(self):
        code, _, raw = self.request("GET", "/platform/health")
        health = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual(health["schema"], "agilex-platform-health-v1")
        self.assertEqual(health["protocol_version"], 1)
        self.assertEqual(health["readiness"], "management_only")
        self.assertEqual(self.upstream.calls, [])

    def test_status_sanitizes_and_keeps_models_off_healthy(self):
        state = self.status()
        self.assertEqual(state["runtime_session"]["components"]["models"]["FP32"]["status"], "OFF")
        self.assertIsNone(state["current_failure"])
        self.assertEqual(state["historical_failure"]["id"], "old-failure")
        self.assertEqual(state["runtime_session"]["components"]["storage"]["status"], "READY")
        self.assertEqual(state["legacy_guard"]["execution_admission_granted"], False)
        raw = json.dumps(state)
        for secret in ("SECRET", "csrf_token", "password", "config"):
            self.assertNotIn(secret, raw)

    def test_disconnect_discards_previous_ready(self):
        self.assertEqual(self.status()["upstream"]["status"], "ONLINE")
        self.upstream.mode = "disconnect"
        state = self.status()
        self.assertEqual(state["upstream"]["status"], "OFFLINE")
        components = state["runtime_session"]["components"]
        for name in ("hub", "robot_link", "shared_runner", "v2_runner", "storage"):
            self.assertEqual(components[name]["status"], "UNKNOWN")
        self.assertEqual(components["models"]["FP32"]["status"], "UNKNOWN")
        self.assertEqual(state["runtime_session"]["ownership"]["status"], "UNKNOWN")
        self.assertIsNone(state["runtime_session"]["boot"]["robot_boot_id"])
        self.assertEqual(state["validation_record"]["status"], "UNKNOWN")

    def test_stale_legacy_subcomponents_not_ready(self):
        self.upstream.state["precision_workbench"]["fresh"] = False
        self.upstream.state["precision_workbench"]["v2_profile"]["fresh"] = False
        state = self.status()
        self.assertEqual(state["runtime_session"]["components"]["models"]["FP32"]["status"], "UNKNOWN")
        self.assertEqual(state["runtime_session"]["components"]["shared_runner"]["status"], "UNKNOWN")
        self.assertEqual(state["validation_record"]["status"], "UNKNOWN")

    def test_unexpected_protocol_blocks(self):
        self.upstream.mode = "html"
        self.assertEqual(self.status()["upstream"]["status"], "PROTOCOL_ERROR")
        self.upstream.mode = "ok"
        self.upstream.state = {"connected": True}
        self.assertEqual(self.status()["upstream"]["status"], "PROTOCOL_ERROR")

    def test_redirect_not_followed(self):
        self.upstream.mode = "redirect"
        state = self.status()
        self.assertEqual(state["upstream"]["error_code"], "UPSTREAM_REDIRECT_REFUSED")
        self.assertEqual(len(self.upstream.calls), 1)
        code, _, raw = self.request("GET", "/workbench/")
        self.assertEqual(code, 502)
        self.assertIn(b"REDIRECT_REFUSED", raw)

    def test_timeout_is_bounded(self):
        self.upstream.mode = "slow"
        started = time.monotonic()
        state = self.status()
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertIn(state["upstream"]["status"], ("OFFLINE", "PROTOCOL_ERROR"))

    def test_new_assets_and_legacy_absolute_assets_are_distinct(self):
        self.assertEqual(self.request("GET", "/")[2], b"new-management")
        self.assertIn(b"fixture", self.request("GET", "/platform-assets/platform.mjs")[2])
        self.assertEqual(self.request("GET", "/workbench/")[2], b"legacy:/")
        self.assertEqual(self.request("GET", "/workbench/app.js?v=1")[2], b"legacy:/app.js?v=1")
        self.assertEqual(self.request("GET", "/app.js?v=2")[2], b"legacy:/app.js?v=2")
        self.assertEqual(self.request("GET", "/api/jobs/fixture")[2], b"legacy:/api/jobs/fixture")

    def test_traversal_and_absolute_targets_blocked(self):
        for path in ("/platform-assets/../secret", "/%2e%2e/secret", "/%252e%252e/secret",
                     "//example.com/path", "/a%5cb", "http://example.com/a"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 400)
        self.assertEqual(self.upstream.calls, [])

    def test_wrong_host_and_origin_block_before_upstream(self):
        self.assertEqual(self.request("GET", "/", headers={"Host": "evil.test"})[0], 421)
        headers = {**self.post_headers(), "Origin": "http://evil.test"}
        self.assertEqual(self.request("POST", "/api/jobs", b"{}", headers)[0], 403)
        self.assertEqual(self.upstream.calls, [])

    def test_post_allowlist_and_size_enforced(self):
        self.assertEqual(self.request("POST", "/api/unknown", b"{}", self.post_headers())[0], 404)
        self.assertEqual(self.request("POST", "/api/jobs?x=1", b"{}", self.post_headers())[0], 404)
        self.assertEqual(self.request("POST", "/api/jobs", b"x" * 16385, self.post_headers())[0], 413)
        self.assertEqual(self.request("POST", "/api/jobs", b"[]", self.post_headers())[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", b'{"a":1,"a":2}', self.post_headers())[0], 400)
        self.assertEqual(self.upstream.calls, [])

    def test_post_requires_token_and_plain_length(self):
        headers = self.post_headers()
        headers.pop("X-Tiger-Token")
        self.assertEqual(self.request("POST", "/api/jobs", b"{}", headers)[0], 403)
        headers = {**self.post_headers(), "Transfer-Encoding": "chunked"}
        self.assertEqual(self.request("POST", "/api/jobs", b"{}", headers)[0], 400)
        self.assertEqual(self.upstream.calls, [])

    def test_explicit_post_forwarded_exactly_once_with_original_guard_token(self):
        body = b'{"action":"connect","params":{}}'
        code, _, _ = self.request("POST", "/api/jobs", body, self.post_headers())
        self.assertEqual(code, 202)
        self.assertEqual(len(self.upstream.calls), 1)
        method, path, headers, raw = self.upstream.calls[0]
        self.assertEqual((method, path, raw), ("POST", "/api/jobs", body))
        self.assertEqual(headers["Origin"], f"http://127.0.0.1:{self.upstream.server_port}")
        self.assertEqual(headers["Host"], f"127.0.0.1:{self.upstream.server_port}")
        self.assertEqual(headers["X-Tiger-Token"], "fixture-token")

    def test_ambiguous_post_result_never_retried(self):
        self.upstream.mode = "disconnect"
        code, _, raw = self.request("POST", "/api/stop", b"{}", self.post_headers())
        self.assertEqual(code, 502)
        self.assertEqual(len(self.upstream.calls), 1)
        self.assertFalse(json.loads(raw)["request_retried"])

    def test_loopback_bind_and_origin_validation(self):
        self.assertEqual(self.facade.server_address[0], "127.0.0.1")
        for value in ("https://127.0.0.1:8873", "http://example.com:8873",
                      "http://user:pass@127.0.0.1:8873", "http://127.0.0.1:8873/path"):
            with self.assertRaises(ValueError):
                parse_origin(value)


class CacheTests(unittest.TestCase):
    def test_one_second_cache_then_failed_read_clears_old_value(self):
        class Client:
            timeout = 0.1
            count = 0
            def state(self):
                self.count += 1
                if self.count > 1:
                    raise UpstreamError("UPSTREAM_UNAVAILABLE")
                return fixture_state()
        clock = [0.0]
        client = Client()
        cache = StatusCache(client, clock=lambda: clock[0])
        self.assertIsNotNone(cache.read()[0])
        clock[0] = 0.99
        self.assertIsNotNone(cache.read()[0])
        self.assertEqual(client.count, 1)
        clock[0] = 1.0
        self.assertIsNone(cache.read()[0])
        self.assertEqual(client.count, 2)


if __name__ == "__main__":
    unittest.main()
