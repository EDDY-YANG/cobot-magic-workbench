"""Only startup release identity checks; no HTTP listener or production I/O."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import release


class ReleaseIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.temp = Path(cls.tmp.name)
        cls.source = Path(__file__).resolve().parents[1]
        archive = cls.temp / "source.zip"
        manifest = release.build(cls.source, archive, version="0.1.0-test")
        cls.pin = release.digest(release.canonical(manifest))
        cls.stage = release.stage(archive, cls.temp, cls.pin)
        cls.manifest = cls.stage / release.MANIFEST

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_check(self, *, web=None, pin=None, cwd=None):
        script = (
            "import json; from server.release_identity import verify_startup_identity; "
            "print(json.dumps(verify_startup_identity("
            + repr(str(self.manifest)) + ", "
            + repr(str(web or self.stage / "web")) + ", "
            + repr(pin or self.pin) + ")))")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, "-B", "-c", script],
                              cwd=cwd or self.stage, env=env, capture_output=True,
                              text=True, timeout=10)

    def test_actual_staged_modules_and_web_are_verified(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["integrity"], "VERIFIED_AT_STARTUP")
        self.assertEqual(info["manifest_sha256"], self.pin)
        self.assertTrue(info["manifest_pin_verified"])
        self.assertEqual(info["release_id"], self.stage.name)

    def test_invalid_pin_payload_or_import_location_fail_closed(self):
        for options in ({"pin": "0" * 64},
                        {"web": self.source / "web"},
                        {"cwd": self.source}):
            with self.subTest(options=options):
                self.assertNotEqual(self.run_check(**options).returncode, 0)
        target = self.stage / "server/contracts.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# changed after staging\n")
            self.assertNotEqual(self.run_check().returncode, 0)
        finally:
            target.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
