import errno
from pathlib import Path
import sys
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab_stack"))
from agilex_control import cli, hub


class FrontCameraRecoveryTest(unittest.TestCase):
    def camera(self, recover=True):
        return hub.Camera("front" if recover else "right", "serial", Mock(),
                          (lambda: nullcontext(True)) if recover else None)

    def test_one_attempt_after_failure_and_no_retry_after_failed_reopen(self):
        camera = self.camera()
        with patch.object(camera, "_capture", side_effect=[
            hub.CameraUnavailable("detached"), TimeoutError("no frame after reconnect")
        ]) as capture:
            camera.run()
        self.assertEqual([call.args for call in capture.call_args_list], [(False,), (True,)])
        self.assertEqual(camera.recovery_state, "failed")
        self.assertIsNone(camera.frame())

    def test_right_camera_has_no_automatic_retry(self):
        camera = self.camera(recover=False)
        with patch.object(camera, "_capture", side_effect=TimeoutError("failed")) as capture:
            camera.run()
        capture.assert_called_once_with(False)

    def test_reopen_waits_for_identity_and_idle(self):
        camera = self.camera()
        camera.reopen_guard = Mock(side_effect=[nullcontext(False), nullcontext(True)])
        with patch.object(camera.stop_event, "wait", return_value=False), patch.object(
            hub, "capture_device", side_effect=[hub.CameraUnavailable("missing"), *["camera"] * 4]
        ) as device:
            with camera._reopen():
                self.assertEqual(camera.reconnect_attempts, 1)
                self.assertEqual(camera.recovery_state, "opening")
        self.assertEqual(device.call_count, 5)
        self.assertEqual(camera.reopen_guard.call_count, 2)

    def test_fresh_capture_allows_one_retry_after_later_drop(self):
        camera = self.camera()

        def capture(retry):
            if retry and camera.count == 0:
                camera.count = 60
            raise hub.CameraUnavailable("detached")

        with patch.object(camera, "_capture", side_effect=capture) as mock_capture:
            camera.run()
        self.assertEqual([call.args for call in mock_capture.call_args_list], [(False,), (True,), (True,)])


class HubExitTest(unittest.TestCase):
    def test_waits_for_old_process_then_port(self):
        endpoint = Mock()
        endpoint.bind.side_effect = [OSError(errno.EADDRINUSE, "busy"), None]
        sock = Mock()
        sock.__enter__ = Mock(return_value=endpoint)
        sock.__exit__ = Mock(return_value=False)
        with patch.object(cli, "_registered_child", side_effect=[({}, True), ({}, False), ({}, False)]), patch.object(
            cli.socket, "socket", return_value=sock
        ), patch.object(cli.time, "sleep") as pause:
            cli._wait_hub_exit(Path("/test"), 8781)
        self.assertEqual(endpoint.bind.call_count, 2)
        self.assertEqual(pause.call_count, 2)

    def test_timeout_does_not_report_exit(self):
        with patch.object(cli, "_registered_child", return_value=({}, True)):
            with self.assertRaisesRegex(RuntimeError, "old process or listening port"):
                cli._wait_hub_exit(Path("/test"), 8781, timeout_s=0)
