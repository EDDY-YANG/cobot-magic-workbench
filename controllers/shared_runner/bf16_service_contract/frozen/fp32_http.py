"""Sequential, localhost-only HTTP transport for an already loaded FP32 policy.

This module does not load a model, contact a robot, or change hardware settings.
The injected policy owns model/compute precision verification. Transport tests
using a fake policy establish the protocol only, never real model readiness.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
import math
import os
from pathlib import Path
import signal
import socket
import threading
import time
from typing import Any
import warnings

import numpy as np
from PIL import Image, UnidentifiedImageError


HOST = "127.0.0.1"
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_PROMPT_CHARS = 4096
REQUEST_TIMEOUT_SECONDS = 15
ERROR_DRAIN_SECONDS = 0.25
MIN_IMAGE_EDGE = 32
MAX_IMAGE_WIDTH = 1920
MAX_IMAGE_HEIGHT = 1080
ACTION_SHAPE = (50, 7)


class RequestError(ValueError):
    """A malformed client input, with an explicit HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _no_json_constant(value: str) -> None:
    raise RequestError(f"Non-finite JSON constant is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _json_safe(value: Any) -> Any:
    """Serialize small policy metadata without accepting non-finite values."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite policy metadata")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_safe(item) for key, item in value.items()}
    raise ValueError(f"Unsupported policy metadata type: {type(value).__name__}")


def _decode_jpeg(encoded: Any, name: str) -> np.ndarray:
    if not isinstance(encoded, str) or not encoded:
        raise RequestError(f"{name} must be a nonempty base64 JPEG string")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RequestError(f"{name} is not valid base64") from exc
    try:
        # Bounds are checked from the header before decoding image pixels.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format != "JPEG":
                    raise RequestError(f"{name} must contain a JPEG image")
                width, height = image.size
                if not (
                    MIN_IMAGE_EDGE <= width <= MAX_IMAGE_WIDTH
                    and MIN_IMAGE_EDGE <= height <= MAX_IMAGE_HEIGHT
                ):
                    raise RequestError(
                        f"{name} dimensions must be within "
                        f"{MIN_IMAGE_EDGE}..{MAX_IMAGE_WIDTH} pixels wide and "
                        f"{MIN_IMAGE_EDGE}..{MAX_IMAGE_HEIGHT} pixels high"
                    )
                image.load()
                return np.array(image.convert("RGB"), dtype=np.uint8, copy=True)
    except RequestError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise RequestError(f"{name} cannot be decoded as a complete JPEG") from exc


def decode_observation(payload: Any) -> dict[str, Any]:
    """Validate the wire input; do not synthesize missing observations."""
    required = {"state", "front_jpeg_base64", "right_jpeg_base64", "prompt"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise RequestError("Expected exactly state, front_jpeg_base64, right_jpeg_base64, prompt")
    values = payload["state"]
    if (
        not isinstance(values, list)
        or len(values) != 7
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values)
    ):
        raise RequestError("state must be a list of exactly 7 finite numbers")
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            state = np.asarray(values, dtype=np.float32)
    except (ValueError, TypeError, OverflowError) as exc:
        raise RequestError("state values must be representable as finite float32") from exc
    if state.shape != (7,) or not np.isfinite(state).all():
        raise RequestError("state values must be representable as finite float32")
    prompt = payload["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS or "\x00" in prompt:
        raise RequestError(f"prompt must be a nonempty string of at most {MAX_PROMPT_CHARS} characters")
    return {
        "state": state,
        "front": _decode_jpeg(payload["front_jpeg_base64"], "front_jpeg_base64"),
        "right": _decode_jpeg(payload["right_jpeg_base64"], "right_jpeg_base64"),
        "prompt": prompt,
    }


def encode_result(result: Any, inference_wall_ms: float) -> dict[str, Any]:
    """Reject an invalid policy output rather than silently casting it."""
    if not isinstance(result, dict) or "actions" not in result:
        raise ValueError("Policy result must be a dict containing actions")
    actions = result["actions"]
    if not isinstance(actions, np.ndarray) or actions.dtype != np.dtype("float32"):
        raise ValueError("Policy actions must be a float32 numpy array")
    if actions.shape != ACTION_SHAPE or not np.isfinite(actions).all():
        raise ValueError("Policy actions must be finite with shape [50, 7]")
    response: dict[str, Any] = {
        "actions": actions.tolist(),
        "action_dtype": "float32",
        "action_shape": list(ACTION_SHAPE),
        "inference_wall_ms": inference_wall_ms,
    }
    for name in ("metadata", "timing"):
        if name in result:
            response[name] = _json_safe(result[name])
    return response


def _health(policy: Any) -> dict[str, Any]:
    return {
        "ready": True,
        "local_only": True,
        "contract": {
            "parameter_dtype": "float32",
            "compute_dtype": "float32",
            "state_dtype": "float32",
            "action_dtype": "float32",
            "action_shape": list(ACTION_SHAPE),
            "model_precision_verification_owner": "injected_policy_loader",
        },
        "policy": _json_safe(policy.metadata),
    }


class FP32HTTPServer(HTTPServer):
    """Single request at a time: no concurrent model calls or robot methods."""

    allow_reuse_address = True

    def __init__(self, policy: Any, port: int):
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer in 0..65535")
        if not callable(getattr(policy, "infer", None)) or not isinstance(getattr(policy, "metadata", None), dict):
            raise TypeError("policy must provide infer(observation) and a metadata dict")
        _health(policy)  # Fail before binding if the metadata cannot be serialized.
        self.policy = policy
        super().__init__((HOST, port), FP32RequestHandler)


class FP32RequestHandler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes connections after responses, bounding a single caller's hold.
    protocol_version = "HTTP/1.0"
    server_version = "Pi05LocalFP32/1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: Any) -> None:
        # No observations, prompts, actions, or raw HTTP request paths are logged.
        pass

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        if status >= 400:
            self._finish_rejected_connection()

    def _finish_rejected_connection(self) -> None:
        """Deliver the rejection before closing a socket with unread input.

        Header-level rejection can race a client's separate body send. Closing
        immediately with unread bytes may reset the connection (WinError10053)
        and discard the HTTP error. Half-close the response first, then discard
        at most 8MiB/250ms of input without parsing, logging, or calling policy.
        The bounded drain happens only after the complete error is sent.
        """
        try:
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return  # The caller has already disconnected.
        deadline = time.monotonic() + ERROR_DRAIN_SECONDS
        remaining_bytes = MAX_BODY_BYTES
        while remaining_bytes:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            try:
                self.connection.settimeout(remaining_seconds)
                block = self.rfile.read1(min(65536, remaining_bytes))
            except (OSError, TimeoutError):
                break
            if not block:
                break
            remaining_bytes -= len(block)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._reply(404, {"error": "Unknown endpoint"})
            return
        try:
            self._reply(200, _health(self.server.policy))
        except (ValueError, TypeError):
            self._reply(500, {"error": "Policy health metadata is invalid"})

    def _read_observation(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise RequestError("Transfer-Encoding is not supported")
        length_values = self.headers.get_all("Content-Length", [])
        if len(length_values) != 1:
            raise RequestError("Exactly one Content-Length header is required")
        raw_length = length_values[0]
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise RequestError("Content-Length must be a positive integer")
        normalized_length = raw_length.lstrip("0") or "0"
        maximum_length = str(MAX_BODY_BYTES)
        if len(normalized_length) > len(maximum_length) or (
            len(normalized_length) == len(maximum_length) and normalized_length > maximum_length
        ):
            raise RequestError(f"Request body exceeds {MAX_BODY_BYTES} bytes", 413)
        length = int(normalized_length)
        if length <= 0:
            raise RequestError("Request body must not be empty")
        if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
            raise RequestError("Content-Type must be application/json")
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, OSError) as exc:
            raise RequestError("Timed out or failed while reading the request body") from exc
        if len(raw) != length:
            raise RequestError("Incomplete request body")
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                parse_constant=_no_json_constant,
                object_pairs_hook=_unique_json_object,
            )
        except RequestError:
            raise
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise RequestError("Body must be a valid UTF-8 JSON object") from exc
        return decode_observation(payload)

    def do_POST(self) -> None:
        if self.path != "/infer":
            self._reply(404, {"error": "Unknown endpoint"})
            return
        try:
            observation = self._read_observation()
        except RequestError as exc:
            self._reply(exc.status, {"error": str(exc)})
            return
        started = time.perf_counter()
        try:
            result = self.server.policy.infer(observation)
            response = encode_result(result, (time.perf_counter() - started) * 1000)
        except Exception as exc:
            # Never return a partial action or expose input-dependent exception text.
            self._reply(500, {"error": "Policy inference failed or output violated the FP32 contract", "exception_type": type(exc).__name__})
            return
        self._reply(200, response)

    def _method_not_allowed(self) -> None:
        self._reply(405, {"error": "Only GET /health and POST /infer are supported"})

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_CONNECT = _method_not_allowed
    do_TRACE = _method_not_allowed


def make_server(policy: Any, port: int = 8766) -> FP32HTTPServer:
    """Bind only 127.0.0.1; port zero is useful for transport contract tests."""
    return FP32HTTPServer(policy, port)


def _write_ready(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(body, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def serve(policy: Any, port: int = 8766, ready_file: str | Path | None = None) -> None:
    """Run a sequential local server for an already loaded and verified policy.

    ``ready_file`` receives status only, never request data or actions. Ctrl+C
    and SIGTERM in the main thread close the listener and mark that file stopped.
    """
    server = make_server(policy, port)
    ready_path = Path(ready_file) if ready_file is not None else None
    status = {
        **_health(policy),
        "host": HOST,
        "port": server.server_address[1],
        "pid": os.getpid(),
        "at_utc": datetime.now(timezone.utc).isoformat(),
    }
    previous_sigterm = None

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        if ready_path is not None:
            _write_ready(ready_path, status)
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.signal(signal.SIGTERM, stop)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if ready_path is not None:
            _write_ready(ready_path, {**status, "ready": False, "at_utc": datetime.now(timezone.utc).isoformat()})
