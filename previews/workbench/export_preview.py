"""Build a single-file, offline review copy using the existing licensed demo."""
import argparse
import base64
import json
from pathlib import Path


def data_uri(path, media_type):
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def export(destination):
    here = Path(__file__).resolve().parent
    demo = here.parents[1] / "site" / "assets" / "demo"
    provenance = json.loads((demo / "media-provenance.json").read_bytes())
    payload = {
        "provenance": provenance,
        "joints": json.loads((demo / provenance["data_file"]).read_bytes()),
        "poster": data_uri(demo / "preview-0360.jpg", "image/jpeg"),
        "video": data_uri(demo / provenance["video"], "video/mp4"),
    }
    page = (here / "index.html").read_text(encoding="utf-8")
    style = (here / "workbench.css").read_text(encoding="utf-8")
    logic = (here / "workbench.js").read_text(encoding="utf-8")
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    page = page.replace('<link rel="stylesheet" href="workbench.css">', f"<style>{style}</style>")
    page = page.replace('<script src="workbench.js" defer></script>',
                        f"<script>window.WORKBENCH_PREVIEW={serialized};</script><script>{logic}</script>")
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(page)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    export(parser.parse_args().destination)
