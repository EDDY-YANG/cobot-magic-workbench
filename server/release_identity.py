"""Startup-only verification of the actual facade payload, not another artifact."""
from pathlib import Path
import re
import sys

from tools import release


def verify_startup_identity(manifest_path, web_root, expected_manifest_sha256=None,
                            requested_release_id="development-unreleased"):
    if manifest_path is None:
        if expected_manifest_sha256 is not None:
            raise ValueError("A manifest SHA requires --release-manifest")
        return {"release_id": requested_release_id, "integrity": "UNVERIFIED",
                "manifest_sha256": None, "manifest_pin_verified": False}
    if expected_manifest_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256):
        raise ValueError("Expected manifest SHA must be 64 lowercase hex characters")
    manifest_path = release.no_symlinks(manifest_path)
    if manifest_path.name != release.MANIFEST or not manifest_path.is_file():
        raise ValueError("Expected staged release-manifest.json")
    root = manifest_path.parent.resolve(strict=True)
    if Path(web_root).resolve(strict=True) != root / "web":
        raise ValueError("web-root must be the verified payload web directory")
    # All actual imports used by the server must come from this payload. A
    # valid ZIP or another staged tree cannot authenticate the running source.
    import server
    from server import contracts, main
    loaded = {
        "server/__init__.py": server.__file__,
        "server/main.py": main.__file__,
        "server/contracts.py": contracts.__file__,
        "server/release_identity.py": __file__,
        "tools/release.py": release.__file__,
    }
    entry = sys.modules.get("__main__")
    if getattr(entry, "__package__", None) == "server":
        if Path(entry.__file__).resolve(strict=True) != root / "server/main.py":
            raise ValueError("Actual server entry is outside the verified payload")
    for relative, actual in loaded.items():
        if Path(actual).resolve(strict=True) != root / relative:
            raise ValueError("Imported source is outside the verified payload: " + relative)
    manifest = release.verify(root, expected_manifest_sha256)
    if manifest["package"] != "source":
        raise ValueError("Facade requires a source release")
    members = {row["path"] for row in manifest["files"]}
    if not set(loaded).issubset(members) or "web/index.html" not in members:
        raise ValueError("Required facade modules or web entry are absent from manifest")
    if requested_release_id not in ("development-unreleased", manifest["release_id"]):
        raise ValueError("Requested release ID differs from verified manifest")
    return {"release_id": manifest["release_id"], "integrity": "VERIFIED_AT_STARTUP",
            "manifest_sha256": release.digest(release.canonical(manifest)),
            "manifest_pin_verified": expected_manifest_sha256 is not None}
