"""Six targeted offline tests. Temporary bytes stay inside this checkout."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from tools import release

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        area = ROOT / "build"
        area.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="release-test-", dir=area)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "client").mkdir(parents=True)
        (self.source / "web").mkdir()
        (self.source / "server").mkdir()
        (self.source / "client/gateway.py").write_text("print('client')\n")
        (self.source / "client/client.example.json").write_text('{"host":"example.invalid"}\n')
        (self.source / "web/app.js").write_text("'use strict';\n")
        (self.source / "server/app.py").write_text("READY = False\n")

    def build(self, package="source", name="bundle.zip"):
        output = self.root / name
        release.build(self.source, output, package, "0.1.0-alpha.1")
        return output

    def zip_members(self, output, members):
        with zipfile.ZipFile(output, "w") as archive:
            for name, raw in members:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, raw)

    def test_deterministic_build_and_local_files_excluded(self):
        (self.source / "client/client.local.json").write_text('{"password":"local-only"}')
        (self.source / "client/debug.log").write_text("not shipped")
        (self.source / "client/weights").mkdir()
        (self.source / "client/weights/data.py").write_text("not shipped")
        first = self.build(name="one.zip")
        second = self.build(name="two.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        manifest = release.verify(first)
        self.assertEqual(len(manifest["files"]), 4)
        self.assertIsNone(manifest["source_revision"])

    def test_client_scope_has_no_server_or_web(self):
        archive = self.build("client")
        manifest = release.verify(archive)
        self.assertEqual(set(manifest["roles"]), {"client"})
        self.assertTrue(all(row["path"].startswith("client/") for row in manifest["files"]))
        self.assertEqual(len(manifest["files"]), 2)

    def test_tamper_and_wrong_trusted_manifest_refused(self):
        archive = self.build()
        members = release.read_package(archive)
        members["client/gateway.py"] += b"# modified\n"
        changed = self.root / "changed.zip"
        self.zip_members(changed, members.items())
        with self.assertRaises(release.ReleaseError):
            release.verify(changed)
        with self.assertRaises(release.ReleaseError):
            release.verify(archive, "0" * 64)

    def test_missing_unexpected_and_duplicate_files_refused(self):
        archive = self.build()
        original = release.read_package(archive)
        missing = dict(original)
        del missing["web/app.js"]
        unexpected = {**original, "web/extra.js": b""}
        duplicate = [*original.items(), ("web/app.js", original["web/app.js"])]
        for index, members in enumerate((missing.items(), unexpected.items(), duplicate)):
            with self.subTest(index=index):
                path = self.root / f"invalid-{index}.zip"
                with __import__("warnings").catch_warnings():
                    __import__("warnings").simplefilter("ignore", UserWarning)
                    self.zip_members(path, members)
                with self.assertRaises(release.ReleaseError):
                    release.verify(path)

    def test_traversal_symlink_and_nonportable_paths_refused(self):
        archive = self.build()
        members = release.read_package(archive)
        for name in ("../escape.py", "/absolute.py", "web\\escape.py", "web/CON.py"):
            with self.subTest(name=name):
                bad = self.root / ("bad-" + str(len(name)) + ".zip")
                self.zip_members(bad, [*members.items(), (name, b"")])
                with self.assertRaises(release.ReleaseError):
                    release.verify(bad)
        link = self.source / "client/link.py"
        link.symlink_to(self.source / "server/app.py")
        with self.assertRaises(release.ReleaseError):
            self.build(name="linked.zip")

    def test_stage_idempotent_verified_and_never_overwrites_tampering(self):
        archive = self.build()
        destination = self.root / "releases"
        destination.mkdir()
        manifest = release.verify(archive)
        expected = release.digest(release.canonical(manifest))
        first = release.stage(archive, destination, expected)
        self.assertEqual(release.verify(first, expected), manifest)
        self.assertEqual(release.stage(archive, destination, expected), first)
        target = first / "client/gateway.py"
        target.write_text("modified by external actor\n")
        with self.assertRaises(release.ReleaseError):
            release.stage(archive, destination, expected)
        self.assertEqual(target.read_text(), "modified by external actor\n")


if __name__ == "__main__":
    unittest.main()
