"""Bounded, evidenced finalization of an explicitly owned USB recording.

Only calls the existing sensor hub. No CAN/motor/mode/camera command is added.
HTTP 409 means waiting only for the two documented recorder join-timeouts;
other errors remain failures. Terminal status and the actual saved USB receipt
must agree before recording finalization can be accepted.
"""
import json
import math
from pathlib import Path
import time
import urllib.error

OWNED_RECORDING_STOP_PROTOCOL = 'tiger-owned-recording-stop-v1'


def _usb_file(path, usb):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(usb.resolve()):
        raise RuntimeError("Recording receipt must be a real file on verified USB: " + str(path))
    return path


class HubRpcError(RuntimeError):
    """Retain the actual error JSON; HTTP 409 alone cannot explain cleanup."""
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(json.dumps(receipt, ensure_ascii=False))


def preserving_rpc(raw_rpc, cfg, path, data=None, timeout=20):
    try:
        return raw_rpc(cfg, path, data, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read(65537)
        bounded = body[:65536].decode("utf-8", errors="replace")
        try:
            parsed = json.loads(bounded)
        except json.JSONDecodeError:
            parsed = None
        raise HubRpcError({"path": path, "http_status": exc.code,
            "reason": exc.reason, "response_json": parsed,
            "response_text": bounded if parsed is None else None,
            "response_truncated": len(body) > 65536}) from exc


def stop_owned_recording(cfg, rpc, expected_path, *, timeout=300, monotonic=time.monotonic,
                         sleep=time.sleep, read_final=None):
    """One stop request, then verified finalization on actual USB.

    timeout bounds the initial cleanup wait. A healthy async-flush recorder may
    keep saving beyond it; each HTTP request remains bounded and only the same
    owned episode can finish successfully.

    A join timeout can return HTTP 409 after production has already stopped.
    It is retained as evidence, never treated as either success or data loss.
    Only the same owned episode becoming inactive AND saved can succeed.
    """
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("Finalization timeout must be finite, positive and at most 300 seconds")
    if not isinstance(expected_path,str) or not expected_path:
        raise ValueError('Owned recording path must be a nonempty string')
    def actual_final(path):
        from agilex_control.storage import verify_usb
        usb = verify_usb(cfg)
        final_path = _usb_file(Path(path) / "operation.json", usb)
        return json.loads(final_path.read_text(encoding="utf-8"))
    read_final = read_final or actual_final
    started = monotonic()
    deadline = started + timeout
    evidence = {"owned_path": expected_path, "timeout_s": timeout, "stop_requests": 0,
                "status_polls": 0, "http_errors": [], "completed": False,
                "healthy": False, "failure": None}
    waiting_errors = ("Writer still flushing; not saved yet", "Sensor producer still stopping; not saved yet")
    try:
        current = rpc(cfg, "/status?compact=1", timeout=min(2, timeout))["recording"]
        evidence["before_stop"] = current
        if current.get("path") != expected_path:
            raise RuntimeError("Recording owner/path changed; no stop sent to another episode")
        if current.get("active") is True:
            if current.get('owned_stop_protocol') != OWNED_RECORDING_STOP_PROTOCOL:
                raise RuntimeError('Active Hub does not prove atomic owned-stop protocol; no stop sent')
            evidence["stop_requests"] = 1
            try:
                current = rpc(cfg, "/record/stop", {"expected_path":expected_path}, timeout=min(22, max(.1, deadline - monotonic())))
                evidence["stop_response"] = current
                if (current.get('owned_stop_protocol') != OWNED_RECORDING_STOP_PROTOCOL or
                        current.get('stop_owner_verified') is not True or
                        current.get('stopped_expected_path') != expected_path or
                        current.get('path') != expected_path):
                    raise RuntimeError('Stop response lacks exact owner confirmation; cannot claim owned stop')
            except HubRpcError as exc:
                evidence["http_errors"].append(exc.receipt)
                payload = exc.receipt.get("response_json")
                actual_error = payload.get("error", "") if isinstance(payload, dict) else ""
                if exc.receipt["http_status"] != 409 or not any(text in actual_error for text in waiting_errors):
                    raise
                print("录制采样停止请求已发出；U盘仍在保存。等待完整回执，不发送机械臂指令。", flush=True)
            except (TimeoutError, OSError) as exc:
                # Outcome unknown: observe it, never repeatedly POST to a changed owner.
                evidence["stop_transport_error"] = repr(exc)
        while (monotonic() < deadline or
               (current.get("saving") is True and
                (current.get("recorder_io") or {}).get("schema") == "tiger-recorder-async-flush-v1" and
                current.get("error") is None and current.get("overflows") == 0)):
            remaining = max(.1, deadline - monotonic())
            if current.get("saving") and (current.get("recorder_io") or {}).get("schema") == "tiger-recorder-async-flush-v1":
                remaining = 2.0
                evidence["extended_for_background_finalization"] = True
            current = rpc(cfg, "/status?compact=1", timeout=min(2, remaining))["recording"]
            evidence["status_polls"] += 1
            evidence["last_recording_status"] = current
            if current.get("path") != expected_path:
                raise RuntimeError("Recording episode changed during finalization; cannot claim owned save")
            if current.get("active") is False:
                # Read only the saved actual episode receipt, never synthesize a terminal status.
                final = read_final(expected_path)
                evidence["episode_finalization"] = final
                evidence["completed"] = True
                if (current.get("status") != "RECORDED_NATIVE_REVIEW_REQUIRES_QC" or
                        final.get("status") != "RECORDED_NATIVE_REVIEW_REQUIRES_QC" or
                        current.get("error") or current.get("overflows") or final.get("failure") or
                        final.get("queue_overflows") or current.get("queue_depth") != 0 or
                        current.get("raw_CAN_queue_depth") != 0 or not final.get("samples")):
                    raise RuntimeError("Owned recording ended with failed/incomplete final evidence")
                if (final.get("raw_storage") != expected_path or
                        final.get("samples") != current.get("samples") or
                        final.get("camera_frame_counts") != current.get("camera_frame_counts") or
                        final.get("raw_CAN_counts") != current.get("raw_CAN_counts")):
                    raise RuntimeError("Saved owned episode identity/counts differ from terminal recorder status")
                evidence["healthy"] = True
                break
            sleep(min(.5, remaining))
        if not evidence["completed"]:
            raise RuntimeError("Owned recording did not become inactive and saved within cleanup deadline")
    except Exception as exc:
        evidence["failure"] = repr(exc)
    evidence["elapsed_s"] = monotonic() - started
    return evidence
