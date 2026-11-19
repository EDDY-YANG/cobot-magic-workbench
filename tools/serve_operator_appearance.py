"""Apply published presentation assets to the existing operator HTTP services.

Installation paths and source pins live in an external deployment configuration.
The original process entry, model/robot clients and POST handlers own all actions.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ASSET_PATHS = {
    '/api/appearance/operator-theme.css': ('operator-theme.css', 'text/css; charset=utf-8'),
    '/api/appearance/operator-theme.js': ('operator-theme.js', 'text/javascript; charset=utf-8'),
}


def inject_html(raw):
    text = raw.decode('utf-8')
    if text.count('</body>') != 1:
        raise ValueError('Expected one workbench HTML body')
    if 'src="/api/appearance/operator-theme.js"' in text:
        return raw
    tags = ('<link rel="stylesheet" href="/api/appearance/operator-theme.css">'
            '<script defer src="/api/appearance/operator-theme.js"></script>')
    return text.replace('</body>', tags+'</body>', 1).encode('utf-8')


def load_installation(path):
    config = json.loads(path.read_text())
    if config['kind'] not in ('workbench', 'review'):
        raise ValueError('Unknown UI service')
    for filename, expected in config['pins'].items():
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
            raise ValueError('Installation source mismatch: '+filename)
    entry = Path(config['entry']).resolve(strict=True)
    if str(entry) not in config['pins']:
        raise ValueError('Unpinned entry point')
    # Resolve only the published theme bundled next to this tool.
    assets = Path(__file__).resolve().parents[1]/'lab_stack'/'web'
    static = {route: ((assets/name).read_bytes(), mime)
              for route, (name, mime) in ASSET_PATHS.items()}
    sys.path.insert(0, str(entry.parent))
    spec = importlib.util.spec_from_file_location('_original_operator_entry', entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return config, module, static


def decorate_workbench(module, static):
    original_assets = module.assets
    def assets(root):
        result = original_assets(root)
        for path in ('/', '/index.html'):
            raw, mime = result[path]
            result[path] = (inject_html(raw), mime)
        result.update(static)
        return result
    module.assets = assets


def decorate_review(module, static):
    original_handler = module.Handler
    original_index = (module.HERE/'web'/'index.html').read_bytes()
    index = inject_html(original_index)
    class Handler(original_handler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if self.valid_host() and (path in static or path in ('/', '/index.html')):
                raw, mime = static.get(path, (index, 'text/html; charset=utf-8'))
                self.send_headers(200, mime, len(raw))
                self.wfile.write(raw)
                return
            return super().do_GET()
    module.Handler = Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installation', type=Path, required=True)
    args = parser.parse_args()
    config, module, static = load_installation(args.installation)
    if config['kind'] == 'workbench':
        decorate_workbench(module, static)
    else:
        decorate_review(module, static)
    sys.argv = [config['entry'], *config['argv']]
    module.main()


if __name__ == '__main__':
    main()
