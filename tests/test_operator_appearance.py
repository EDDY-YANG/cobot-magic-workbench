"""Check that appearance serving preserves the original action dispatch."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('appearance', ROOT/'tools/serve_operator_appearance.py')
appearance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appearance)


class AppearanceTests(unittest.TestCase):
    def test_html_injection_leaves_original_body_bytes_intact(self):
        raw = b'<html><body><button id="stop">STOP</button></body></html>'
        result = appearance.inject_html(raw)
        insertion = result[result.index(b'<link'):result.index(b'</body>')]
        self.assertEqual(result.replace(insertion, b''), raw)
        self.assertEqual(appearance.inject_html(result), result)
        with self.assertRaises(ValueError):
            appearance.inject_html(b'<body>missing close')

    def test_workbench_preserves_actions_and_existing_feedback_guard(self):
        original = {'/':(b'<body>home</body>', 'text/html'),
                    '/index.html':(b'<body>home</body>', 'text/html'),
                    '/app.js':((ROOT/'lab_stack/web/app.js').read_bytes(), 'text/javascript')}
        module = SimpleNamespace(assets=lambda root: original.copy())
        appearance.decorate_workbench(module, {'/api/appearance/test.css':(b'css', 'text/css')})
        served = module.assets(Path('.'))
        app = served['/app.js'][0].decode()
        self.assertIn('function rightSharedFeedbackBlock(', app)
        original_actions = [line for line in original['/app.js'][0].decode().splitlines()
                            if '/api/jobs' in line or '/api/stop' in line]
        self.assertGreaterEqual(len(original_actions), 2)
        for line in original_actions:
            self.assertIn(line, app)
        self.assertEqual(served['/app.js'][1], original['/app.js'][1])
        self.assertIn('/api/appearance/test.css', served)

    def test_workbench_start_refuses_missing_recovery_helper_pins(self):
        config = {"kind": "workbench", "argv": ["--root", str(ROOT/"lab_stack")],
                  "entry": "unused", "pins": {}}
        module = SimpleNamespace(main=Mock())
        with patch.object(appearance, "load_installation", return_value=(config, module, {})):
            with patch.object(sys, "argv", ["appearance", "--installation", "unused.json"]):
                with self.assertRaisesRegex(ValueError, "Unpinned USB reconnect helper"):
                    appearance.main()
        module.main.assert_not_called()


    def test_display_snapshot_preserves_handler_and_checks_native_source(self):
        native = {"/": (b"old page", "text/html"), "/index.html": (b"old page", "text/html"),
                  "/app.js": (b"old js", "text/javascript"), "/other": (b"other", "text/plain")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workbench_display": {}, "pins": {}}
            for url in ("/index.html", "/app.js"):
                path = root / url.lstrip("/")
                path.write_bytes(("versioned " + url).encode())
                config["pins"][str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
                config["workbench_display"][url] = {"file": str(path),
                    "base_sha256": hashlib.sha256(native[url][0]).hexdigest()}
            module = SimpleNamespace(assets=lambda root: native.copy(), submit=Mock())
            appearance.install_display_snapshot(module, root, config)
            self.assertEqual(module.assets(root)["/"], (b"versioned /index.html", "text/html"))
            self.assertEqual(module.assets(root)["/other"], native["/other"])
            module.submit.assert_not_called()
            config["workbench_display"]["/app.js"]["base_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "baseline"):
                appearance.install_display_snapshot(SimpleNamespace(assets=lambda _:native.copy()),root,config)
            del config["pins"][str((root/"app.js").resolve())]
            with self.assertRaisesRegex(ValueError, "Unpinned"):
                appearance.install_display_snapshot(SimpleNamespace(assets=lambda _:native.copy()),root,config)


    def test_review_inherits_post_and_all_unrelated_routes(self):
        events = []
        class Original:
            def valid_host(self):
                return self.host_ok
            def do_POST(self):
                return 'original POST'
            def do_GET(self):
                events.append(self.path)
                return 'original GET'
            def send_headers(self, *args):
                self.headers = args
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'web').mkdir()
            (root/'web/index.html').write_bytes(b'<body>review</body>')
            module=SimpleNamespace(HERE=root, Handler=Original)
            appearance.decorate_review(module, {})
            self.assertIs(module.Handler.do_POST, Original.do_POST)
            request=module.Handler()
            request.path='/api/catalog'
            request.host_ok=True
            self.assertEqual(request.do_GET(), 'original GET')
            request.path='/'
            request.host_ok=False
            self.assertEqual(request.do_GET(), 'original GET')
            self.assertEqual(events, ['/api/catalog', '/'])


if __name__ == '__main__':
    unittest.main()
