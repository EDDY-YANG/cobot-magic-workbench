# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Serve the existing workbench with the right-side DAgger extension."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

FEATURE_ROOT = Path(__file__).resolve().parents[1] / "lab_stack"
SOURCE_PINS = {}


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError("Workbench integration point changed: " + old)
    return source.replace(old, new, 1)


def assets(root):
    for name, expected in SOURCE_PINS.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Workbench source changed: " + name)
    web = FEATURE_ROOT / "web"
    index = (root / "web/index.html").read_text()
    app = (root / "web/app.js").read_text()
    nav = ('<button class="nav-item" data-page="dagger" aria-label="右侧 DAgger">'
           '<span class="nav-icon">⇄</span><span>DAgger 介入</span></button>')
    index = replace_once(index, '<a class="nav-item" href="http://127.0.0.1:8891/',
                         nav + '\n      <a class="nav-item" href="http://127.0.0.1:8891/')
    index = replace_once(index, "</main>", (web / "dagger-section.html").read_text() + "\n</main>")
    index = replace_once(index, "</head>", '<link rel="stylesheet" href="/api/dagger/style.css"></head>')
    app = 'import {bindDagger,displayDagger} from "/api/dagger/view.mjs";\n' + app
    app = replace_once(app, "const pageNames = {", 'const pageNames = {\n  dagger: "右侧 DAgger",')
    app = replace_once(app, "function renderState(state) {", "function renderState(state) {\n  displayDagger(state.dagger);")
    app = app.replace("backendOnline = false;", "backendOnline = false;\n  displayDagger(null);")
    app += "\nbindDagger(api);\n"
    return {
        "/": (index.encode(), "text/html; charset=utf-8"),
        "/index.html": (index.encode(), "text/html; charset=utf-8"),
        "/app.js": (app.encode(), "text/javascript; charset=utf-8"),
        "/api/dagger/style.css": ((web / "dagger.css").read_bytes(), "text/css; charset=utf-8"),
        "/api/dagger/view.mjs": ((web / "dagger-view.mjs").read_bytes(), "text/javascript; charset=utf-8"),
    }


def main():
    global SOURCE_PINS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8873)
    parser.add_argument("--source-pins", type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if args.source_pins:
        SOURCE_PINS = json.loads(args.source_pins.read_text())
    sys.path.insert(0, str(root))
    import agilex_control
    # Existing installation modules remain the authority for its transport and services.
    feature_modules = str(FEATURE_ROOT / "agilex_control")
    if feature_modules not in agilex_control.__path__:
        agilex_control.__path__.append(feature_modules)
    from agilex_control.web_backend import make_server
    from agilex_control.right_dagger_web import DaggerWorkbench
    static = assets(root)
    cfg = json.loads((root / "2026-10-06-14-05-config.json").read_text())
    app = DaggerWorkbench(cfg, root)
    try:
        with make_server(app, args.port) as server:
            original = server.RequestHandlerClass
            class Handler(original):
                def do_GET(self):
                    path = urlsplit(self.path).path
                    if path in static and self.valid_host():
                        return self.reply(200, *static[path])
                    return super().do_GET()
            server.RequestHandlerClass = Handler
            server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


if __name__ == "__main__":
    main()
