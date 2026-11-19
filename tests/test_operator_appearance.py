"""Check that appearance serving preserves the original action dispatch."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

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

    def test_workbench_retains_original_javascript(self):
        original = {'/':(b'<body>home</body>', 'text/html'),
                    '/index.html':(b'<body>home</body>', 'text/html'),
                    '/app.js':(b'original handlers', 'text/javascript')}
        module = SimpleNamespace(assets=lambda root: original.copy())
        appearance.decorate_workbench(module, {'/api/appearance/test.css':(b'css', 'text/css')})
        served = module.assets(Path('.'))
        self.assertEqual(served['/app.js'], original['/app.js'])
        self.assertIn('/api/appearance/test.css', served)

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
