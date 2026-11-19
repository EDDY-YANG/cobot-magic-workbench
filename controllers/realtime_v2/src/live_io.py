"""Loaded-model and existing-Hub I/O for isolated V2 NO_SEND.

No model launch/select, camera reader, SDK import or CAN writer exists here.
Canonical snapshot/storage validators remain authoritative. Frozen real-input
comparisons are explicitly distinguished from fresh continuous observations.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import http.client
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
import uuid


OWNER_NAMES = ("h50-deployment", "fp32-shadow-run", "writer-can_right")
RTC_SCHEMA = "tiger-fp32-h50-rtc-v1"
FRESH = "FRESH_EXISTING_HUB_OBSERVATION"
FROZEN = "FROZEN_REAL_FIRST_PACKET_COMPARISON"


class LiveIOError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise LiveIOError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_selected_config(cfg):
    """Require the actual current runtime selection; never choose/rebase a USB."""
    selected = cfg.get("usb_selection")
    require(isinstance(selected, dict) and selected.get("schema") == "tiger-usb-selection-v1",
            "Actual frozen USB selection required")
    for key in ("mountpoint", "uuid", "major_minor", "source", "device", "physical_usb_path"):
        require(isinstance(selected.get(key), str) and selected[key], "USB selection missing " + key)
    require(cfg.get("usb") == selected["mountpoint"]
            and str(cfg.get("usb_uuid", "")).upper() == selected["uuid"].upper(),
            "Runtime config USB identity differs from selection")
    require(isinstance(cfg.get("hostname"), str) and cfg["hostname"], "Runtime hostname missing")
    require(isinstance(cfg.get("remote_bundle"), str)
            and Path(cfg["remote_bundle"]).is_absolute()
            and ".." not in Path(cfg["remote_bundle"]).parts
            and Path(cfg["remote_bundle"]).is_relative_to(Path(cfg["usb"])),
            "Runtime source/recording bundle must remain on selected USB")
    return deepcopy(selected)


def public_receipt(value):
    """Remove credentials from nested receipts; hashes may identify services."""
    if isinstance(value, dict):
        return {key: public_receipt(item) for key, item in value.items()
                if key.lower() not in ("lease_token", "token", "service_nonce")}
    if isinstance(value, list):
        return [public_receipt(item) for item in value]
    return value


class ModelHTTP:
    """Whitelisted loopback transport with a whole-call watchdog and byte caps."""
    ROUTES = {("GET", "/health"), ("GET", "/status"),
              ("POST", "/infer_rtc"), ("POST", "/lease/acquire"),
              ("POST", "/lease/heartbeat"), ("POST", "/lease/release")}

    def __init__(self, port, timeout=3.0):
        require(port in (18883, 18960, 18961), "Only existing loopback model ports are permitted")
        require(0 < timeout <= 15, "Bounded model timeout required")
        self.port, self.timeout = port, timeout

    def request(self, method, path, payload=None, headers=None):
        require((method, path) in self.ROUTES, "Model mutation/unsupported route forbidden")
        body = None if payload is None else canonical(payload)
        require(body is None or len(body) <= 8 * 1024 * 1024, "Model body exceeds 8 MiB")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        expired = threading.Event()
        socket_lock = threading.Lock()
        deadline_socket = response = timer = None
        deadline = time.monotonic() + self.timeout

        def expire():
            expired.set()
            with socket_lock:
                try:
                    deadline_socket.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
        try:
            conn.connect()
            # HTTP/1.0 closes conn.sock while response.fp still owns the fd.
            # Keep a duplicate so the watchdog interrupts slow headers/bodies.
            deadline_socket = conn.sock.dup()
            remaining = deadline - time.monotonic()
            require(remaining > 0, "HTTP connect exceeded deadline")
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            fields = {"Accept": "application/json", "Connection": "close", **(headers or {})}
            if body is not None:
                fields.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
            conn.request(method, path, body=body, headers=fields)
            require(deadline - time.monotonic() > 0, "HTTP request exceeded deadline")
            if conn.sock is not None:
                conn.sock.settimeout(deadline - time.monotonic())
            response = conn.getresponse()
            length = response.getheader("Content-Length", "")
            require(length.isdecimal() and int(length) <= 4 * 1024 * 1024
                    and not response.getheader("Transfer-Encoding"), "Bounded nonchunked response required")
            require(response.getheader("Content-Type", "").split(";")[0].strip() == "application/json",
                    "JSON response required")
            remaining, raw = int(length), bytearray()
            while remaining:
                left = deadline - time.monotonic()
                require(left > 0 and not expired.is_set(), "HTTP whole-call deadline expired")
                chunk = response.read1(min(65536, remaining))
                require(bool(chunk), "Truncated HTTP response")
                raw.extend(chunk)
                remaining -= len(chunk)

            def pairs(items):
                result = {}
                for key, value in items:
                    require(key not in result, "Duplicate JSON response field")
                    result[key] = value
                return result

            def bad(_):
                raise LiveIOError("Nonfinite JSON response")

            require(time.monotonic() <= deadline and not expired.is_set(), "HTTP whole-call deadline expired")
            value = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad)
            require(isinstance(value, dict), "Object model response required")
            # Do not echo service response text: an error must never expose a lease.
            require(response.status == 200, "Model HTTP rejected: " + str(response.status))
            return value
        finally:
            if timer is not None:
                timer.cancel()
            with socket_lock:
                if deadline_socket is not None:
                    deadline_socket.close()
            if response is not None:
                response.close()
            conn.close()


def make_rtc(previous_actions, *, enabled, trial_index, request_index):
    """Original wire schema; guided-first index zero is intentionally valid."""
    require(type(enabled) is bool, "Explicit RTC enabled flag required")
    for value in (trial_index, request_index):
        require(type(value) is int and 0 <= value <= 1_000_000, "Invalid RTC RNG index")
    known = 35 if enabled else 0
    previous = deepcopy(previous_actions)
    require(isinstance(previous, list) and len(previous) == known, "RTC known prefix length differs")
    require(all(isinstance(row, list) and len(row) == 7 and all(
        type(x) in (int, float) and math.isfinite(x) and abs(x) <= 3.4028234663852886e38
        for x in row) for row in previous), "RTC prefix must be finite 7D rad/m")
    return {"schema": RTC_SCHEMA, "enabled": enabled, "previous_actions": previous,
            "previous_valid_steps": known, "freeze_steps": 12, "execution_label_hz": 18,
            "native_training_hz": 30, "beta": 5.0,
            "trial_index": trial_index, "request_index": request_index}


@dataclass(frozen=True)
class CapturedObservation:
    """Immutable-by-digest capture. Caller persists exact snapshot on verified USB."""
    snapshot: dict
    payload: dict
    ages_at_capture: dict
    captured_monotonic_ns: int
    snapshot_sha256: str
    payload_sha256: str

    def validate_integrity(self):
        require(digest(self.snapshot) == self.snapshot_sha256
                and digest(self.payload) == self.payload_sha256, "Captured observation was modified")

    @property
    def anchor_ns(self):
        return self.snapshot["packet_ready_monotonic_ns"]


@contextmanager
def owned_session(cfg, *, storage=None):
    """Match all three existing runtime locks; no new hardware owner is created."""
    if storage is None:
        from agilex_control import storage
    with ExitStack() as stack:
        for name in OWNER_NAMES:
            stack.enter_context(storage.Ownership(name))
        storage.verify_usb(cfg)
        yield


class ExistingHub:
    """Calls only the existing registered Hub and its owned recording lifecycle."""
    def __init__(self, cfg, *, full_status=False, dependencies=None):
        require(type(full_status) is bool, "Explicit full-status Boolean required")
        self.cfg = deepcopy(cfg)
        # APPLY's original gripper/limit guards need actual raw0x2A8/0x473.
        # This only chooses an existing read-only Hub response, never a reader.
        self.full_status = full_status
        if dependencies is None:
            validate_selected_config(self.cfg)
            from agilex_control.hub import rpc
            from agilex_control.recording_lifecycle import preserving_rpc, stop_owned_recording, OWNED_RECORDING_STOP_PROTOCOL
            from agilex_control.remote_fp32 import validate_snapshot, validate_recording
            from agilex_control.time_contract import capture_clock_domain
            from agilex_control.storage import verify_usb
            dependencies = dict(rpc=lambda cfg, path, data=None, timeout=20:
                                preserving_rpc(rpc, cfg, path, data, timeout),
                                stop_owned=stop_owned_recording,
                                protocol=OWNED_RECORDING_STOP_PROTOCOL,
                                validate_snapshot=validate_snapshot, validate_recording=validate_recording,
                                clock_domain=capture_clock_domain(), verify_usb=verify_usb)
        self.d = dependencies
        self.owned_path = None
        self.producer = None
        self.storage_binding = None

    def rpc(self, path, data=None, timeout=2):
        require(path in ("/status", "/status?compact=1", "/observation/fp32", "/recording/fp32-binding",
                         "/record/start", "/record/stop"), "Unsupported Hub route")
        require(data is None or path in ("/record/start", "/record/stop"), "Hub control mutation forbidden")
        return self.d["rpc"](self.cfg, path, data, timeout=timeout)

    def status(self):
        status = self.rpc("/status" if self.full_status else "/status?compact=1")
        require(isinstance(status.get("producer"), dict) and status["producer"], "Hub producer identity missing")
        require(isinstance(status.get("storage_binding"), dict) and status["storage_binding"], "Hub USB binding missing")
        if self.producer is None:
            self.producer = deepcopy(status["producer"])
            self.storage_binding = deepcopy(status["storage_binding"])
        require(status["producer"] == self.producer, "Existing Hub producer changed")
        require(status["storage_binding"] == self.storage_binding, "Existing Hub USB binding changed")
        return status

    def feedback(self):
        """Actual existing-cache feedback with original kernel receipt times.

        source_ns is the oldest of the four required source streams, not HTTP
        receive time. Six joints are converted deg->rad; gripper stays meters.
        No clipping, synthetic velocity, sensor reader or robot command exists.
        """
        started_ns = time.monotonic_ns()
        status = self.status()
        now_ns = time.monotonic_ns()
        require(now_ns - started_ns <= 200_000_000, "Hub feedback transport exceeded 200 ms")
        if self.owned_path is not None:
            recording = status.get("recording", {})
            require(recording.get("path") == self.owned_path and recording.get("active") is True
                    and recording.get("error") is None and recording.get("overflows") == 0,
                    "Owned recording changed, stopped or failed")
        state = status.get("arms", {}).get("right")
        require(isinstance(state, dict) and state.get("feedback_fresh") is True
                and state.get("observer_running") is True and not state.get("observer_error")
                and not state.get("source_callback_error") and state.get("timestamp_failures") == 0,
                "Actual right feedback missing, stale or unhealthy")
        require(bool(state.get("status_hex")), "Right status feedback missing")
        for key in ("age_joint_s", "age_status_s", "age_gripper_s"):
            age = state.get(key)
            require(type(age) in (int, float) and math.isfinite(age) and 0 <= age <= .2,
                    "Right feedback freshness failed: " + key)
        q, grip = state.get("q_deg"), state.get("grip_m")
        finite = lambda value: type(value) in (int, float) and math.isfinite(value)
        require(isinstance(q, list) and len(q) == 6 and all(finite(x) for x in q) and finite(grip),
                "Actual six joints and linear gripper width required")
        domain = self.d.get("clock_domain", {})
        require(domain.get("identity_verified") is True and domain.get("clock_name") == "monotonic_ns"
                and all(isinstance(domain.get(k), str) and domain[k] for k in ("host_id", "boot_id")),
                "Local feedback clock identity unavailable")
        timing = state.get("source_timing", {})
        source_times, available_times = {}, {}
        for key in ("q12", "q34", "q56", "gripper"):
            item = timing.get(key, {})
            clock_domain = item.get("clock_domain", {})
            require(item.get("mapping_valid") is True and item.get("provenance_complete") is True
                    and item.get("clock_domain_identity_verified") is True
                    and item.get("source_timestamp_kind") == "linux_so_timestampns_kernel_receipt"
                    and clock_domain.get("identity_verified") is True
                    and clock_domain.get("clock_name") == "monotonic_ns"
                    and all(clock_domain.get(k) == domain[k] for k in ("host_id", "boot_id")),
                    "Invalid source clock/provenance for " + key)
            source, available = item.get("source_monotonic_ns_estimate"), item.get("available_monotonic_ns")
            require(type(source) is int and type(available) is int and 0 < source <= available <= now_ns
                    and now_ns - source <= 200_000_000, "Stale/future source time for " + key)
            source_times[key], available_times[key] = source, available
        return {"q_rad_m": [math.radians(x) for x in q] + [grip],
                "source_ns": min(source_times.values()), "source_ns_by_component": source_times,
                "available_monotonic_ns": max(available_times.values()),
                "received_monotonic_ns": now_ns, "producer": deepcopy(self.producer),
                "source_timing": {key: deepcopy(timing[key]) for key in source_times},
                "feedback": deepcopy(state), "hardware_generation_timestamp_available": False,
                "robot_commands": 0}

    def begin_owned_recording(self, task):
        require(self.owned_path is None, "Recording already owned")
        require(isinstance(task, str) and task.strip(), "Explicit recording task required")
        self.d["verify_usb"](self.cfg)
        before = self.status()["recording"]
        require(before.get("active") is False and before.get("saving") is False,
                "Another recording/save is active")
        require(before.get("owned_stop_protocol") == self.d["protocol"], "Owned-stop protocol unavailable")
        previous = before.get("path")
        try:
            response = self.rpc("/record/start", {"task": task}, timeout=20)
            path = response.get("path")
        except BaseException:
            # Unknown POST outcome: only recover ownership if the exact task,
            # new path and producer are visible. Never retry POST or stop others.
            current = self.status().get("recording", {})
            path = current.get("path")
            if isinstance(path, str) and path != previous:
                candidate = Path(path)
                root = Path(self.cfg["usb"])
                self.d["verify_usb"](self.cfg)
                if candidate.is_absolute() and candidate.is_relative_to(root):
                    receipt = candidate / "operation.json"
                    if receipt.is_file() and not receipt.is_symlink() and receipt.resolve().parent == candidate:
                        operation = json.loads(receipt.read_text(encoding="utf-8"))
                        if operation.get("raw_storage") == path and operation.get("task") == task:
                            self._bind_path(path)
            raise
        require(path != previous, "Recording did not allocate a new episode")
        self._bind_path(path)
        return {"owned_recording_path": self.owned_path, "producer": deepcopy(self.producer),
                "storage_binding": deepcopy(self.storage_binding), "robot_commands": 0}

    def _bind_path(self, path):
        require(isinstance(path, str) and path, "Recording path missing")
        episode, root = Path(path), Path(self.cfg["usb"])
        require(episode.is_absolute() and ".." not in episode.parts
                and episode.parent == Path(self.cfg["remote_bundle"]) / "runs"
                and episode.is_relative_to(root), "Recording path escapes owned USB runtime")
        require(episode.is_dir() and not episode.is_symlink() and episode.resolve() == episode
                and episode.stat().st_dev == root.stat().st_dev, "Actual USB recording directory invalid")
        self.owned_path = path

    def capture(self, prompt, journal):
        require(self.owned_path == str(journal.episode_path), "Snapshot journal/recording owner differs")
        self.status()
        snapshot = self.rpc("/observation/fp32")
        frozen, payload, ages = self.d["validate_snapshot"](
            snapshot, prompt, journal, self.d["clock_domain"])
        require(ages["front_age_s"] <= .25 and ages["right_age_s"] <= .25,
                "Camera source exceeds 250 ms freshness")
        self.d["validate_recording"](self.rpc("/recording/fp32-binding"), journal)
        return CapturedObservation(frozen, payload, ages, time.monotonic_ns(), digest(frozen), digest(payload))

    def validate_fresh(self, capture, journal):
        capture.validate_integrity()
        frozen, payload, ages = self.d["validate_snapshot"](
            capture.snapshot, capture.payload["prompt"], journal, self.d["clock_domain"])
        require(digest(payload) == capture.payload_sha256, "Validated snapshot payload changed")
        require(ages["front_age_s"] <= .25 and ages["right_age_s"] <= .25,
                "Camera source exceeds 250 ms freshness")
        self.d["validate_recording"](self.rpc("/recording/fp32-binding"), journal)
        return ages

    def end_owned_recording(self, timeout=300):
        require(self.owned_path is not None, "No recording owned")
        self.status()
        result = self.d["stop_owned"](self.cfg, self.d["rpc"], self.owned_path, timeout=timeout)
        require(result.get("completed") is True and result.get("healthy") is True,
                "Owned recording finalization failed: " + str(result.get("failure")))
        self.owned_path = None
        return result


def _validators(precision):
    from agilex_control.remote_fp32 import validate_result
    if precision == "bf16":
        from bf16_service_contract import contract
        frozen = contract.load_validators(Path(contract.__file__).parent / "frozen")
        return dict(health=contract.validate_health, result=validate_result, rtc=frozen.validate_rtc,
                    response_rtc=contract.validate_response_rtc)
    from agilex_control.h50_runtime import validate_health
    from frozen_rtc.frozen_rtc_core import validate_rtc

    def response_rtc(returned, request):
        expected = {key: request[key] for key in (
            "schema", "enabled", "freeze_steps", "previous_valid_steps", "execution_label_hz",
            "native_training_hz", "beta", "trial_index", "request_index", "trial_seed", "previous_actions_sha256")}
        expected.update(latent_dimensions=32, guidance_error_physical_dimensions=7,
                        prefix_exactly_overwritten=False, latent_extra_dimensions_forced_zero=False,
                        unknown_error_weights_zero=True)
        require(isinstance(returned, dict) and all(type(returned.get(k)) is type(v) and returned[k] == v
                for k, v in expected.items()), "FP32 RTC response differs from request")
    return dict(health=validate_health, result=validate_result, rtc=validate_rtc, response_rtc=response_rtc)


class PinnedLoadedModel:
    """One existing model, exclusive lease, heartbeat, single flight and drain.

    close() refuses release while server inference is busy. Timeout/cancel never
    turns a late result into an accepted plan; heartbeat continues through drain.
    A failed drain raises with credentials/lease retained for a later close().
    """
    def __init__(self, precision, expected_step, *, port=None, transport=None, validators=None):
        require(precision in ("fp32", "bf16"), "Explicit existing precision required")
        require(type(expected_step) is int and expected_step in (2000, 5000, 10000, 15000, 20000),
                "Explicit existing checkpoint required")
        self.precision, self.step = precision, expected_step
        self.http = transport or ModelHTTP(port or (18961 if precision == "bf16" else 18883))
        self.v = validators or _validators(precision)
        self.health = self.identity = self.nonce = self.instance = None
        self.session = self._token = self.trial_index = None
        self._heartbeat_stop = threading.Event()
        self._cancelled = threading.Event()
        self._flight = threading.Lock()
        self._lease_io = threading.Lock()
        self._worker = None
        self.failure = None
        self.last_index = -1
        self.cancel_epoch = 0
        self.last_attempt = None

    def _identity(self, health):
        self.v["health"](health, self.step)
        return digest({**{key: health[key] for key in ("contract", "policy", "rtc", "acceptance_binding")},
                       "service_instance": self._service_instance(
                           health if self.precision == "bf16" else health.get("manager", {}))})

    def _service_instance(self, status):
        if self.precision == "bf16":
            nonce = status.get("service_nonce")
            require(isinstance(nonce, str) and len(nonce) > 16, "BF16 service identity missing")
            return {"service_nonce_sha256": digest(nonce)}
        worker, selection = status.get("worker"), status.get("selection_id")
        require(isinstance(worker, dict) and type(worker.get("pid")) is int and worker["pid"] > 0
                and isinstance(worker.get("boot_id"), str) and worker["boot_id"]
                and isinstance(worker.get("start_ticks"), str) and worker["start_ticks"].isdigit()
                and re.fullmatch("[0-9a-f]{64}", str(worker.get("cmdline_sha256", "")))
                and isinstance(selection, str) and selection,
                "FP32 online blocked: supervisor worker PID/boot/start/command and selection identity required")
        return {"selection_id": selection,
                **{key: worker[key] for key in ("pid", "boot_id", "start_ticks", "cmdline_sha256")}}

    def bind(self):
        require(self.session is None, "Cannot rebind a leased model")
        status = self.http.request("GET", "/status")
        require(status.get("status") == "READY" and status.get("selected_step") == self.step
                and status.get("busy") is False and status.get("lease", {}).get("active") is False,
                "Current loaded model unavailable, changed or leased")
        health = self.http.request("GET", "/health")
        self.identity = self._identity(health)
        self.instance = self._service_instance(status)
        require(self._service_instance(health if self.precision == "bf16" else health.get("manager", {}))
                == self.instance, "Model instance changed between status and health")
        self.nonce = health.get("service_nonce")
        if self.precision == "bf16":
            require(isinstance(self.nonce, str) and len(self.nonce) > 16, "BF16 service identity missing")
        self.health = deepcopy(health)
        return {"step": self.step, "precision": self.precision, "identity_sha256": self.identity,
                "health": public_receipt(health), "instance": deepcopy(self.instance),
                "service_nonce_sha256": digest(self.nonce) if self.nonce else None}

    def check_identity(self):
        health = self.http.request("GET", "/health")
        require(self._identity(health) == self.identity and health.get("service_nonce") == self.nonce,
                "Loaded model/service identity changed")
        require(self._service_instance(health if self.precision == "bf16" else health.get("manager", {}))
                == self.instance, "Loaded model worker instance changed")

    def acquire(self, session_id, trial_index=0):
        require(self.identity is not None and self.session is None, "Bind idle model before acquiring")
        require(isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", session_id),
                "Explicit lease session required")
        require(type(trial_index) is int and 0 <= trial_index <= 1_000_000, "Invalid trial index")
        self.check_identity()
        headers = {"X-Tiger-Service-Nonce": self.nonce} if self.precision == "bf16" else {}
        response = self.http.request("POST", "/lease/acquire",
                                     {"session_id": session_id, "step": self.step, "trial_index": trial_index}, headers)
        token = response.get("lease_token")
        require(isinstance(token, str) and 16 <= len(token) <= 256 and "\n" not in token, "Invalid lease credential")
        # Retain credentials even if later response validation fails so close()
        # can release a lease already granted by the real service.
        self.session, self._token, self.trial_index = session_id, token, trial_index
        require(response.get("step") == self.step and response.get("session_id") == session_id
                and response.get("ttl_s") == 2 and response.get("heartbeat_interval_s") == .5,
                "Lease identity/timing changed")
        require(response.get("trial_index") == trial_index and response.get("trial_seed") == 42 + trial_index
                and response.get("contract_fingerprint") == self.health["acceptance_binding"]["contract_fingerprint"],
                "Lease seed/model contract changed")
        self.last_index = -1
        self.failure = None
        self._heartbeat_stop.clear()
        self._cancelled.clear()
        self._worker = threading.Thread(target=self._heartbeat, name="v2-loaded-model-lease", daemon=True)
        self._worker.start()
        return public_receipt(response)

    def _heartbeat(self):
        while not self._heartbeat_stop.wait(.5):
            try:
                with self._lease_io:
                    if self._heartbeat_stop.is_set():
                        return
                    response = self.http.request("POST", "/lease/heartbeat",
                        {"session_id": self.session, "lease_token": self._token})
                    require(response.get("step", self.step) == self.step, "Lease model changed")
            except Exception:
                self.failure = "Model lease heartbeat failed"
                self.cancel()
                return

    def check_alive(self):
        require(self.session is not None and not self._cancelled.is_set() and self.failure is None,
                self.failure or "Model lease cancelled/unavailable")

    def cancel(self):
        self.cancel_epoch += 1
        self._cancelled.set()

    def infer(self, capture, rtc, *, observation_mode=FRESH, fresh_validator=None):
        """Return actual response and acceptance flags, never dispatch or commit.

        Same observation/seed comparisons use two successive leases and the same
        trial/request indices, because each lease forbids duplicate RNG indices.
        Frozen comparisons preserve their original observation anchor.
        """
        self.check_alive()
        require(self._flight.acquire(blocking=False), "Only one inference in flight")
        started = time.monotonic_ns()
        epoch = self.cancel_epoch
        self.last_attempt = {"status": "STARTED", "request_started_ns": started,
                             "observation_mode": observation_mode, "rtc": deepcopy(rtc), "robot_commands": 0}
        try:
            capture.validate_integrity()
            require(observation_mode in (FRESH, FROZEN), "Explicit observation mode required")
            if observation_mode == FRESH:
                require(callable(fresh_validator), "Continuous inference requires live freshness validation")
                fresh_validator(capture)
            validated = self.v["rtc"](rtc)
            require(validated["trial_index"] == self.trial_index and validated["request_index"] > self.last_index,
                    "RTC trial or monotonic request index differs")
            self.last_index = validated["request_index"]
            self.check_identity()
            payload = {**deepcopy(capture.payload), "rtc": deepcopy(rtc)}
            sent = time.monotonic_ns()
            response = self.http.request("POST", "/infer_rtc", payload,
                {"X-Tiger-Deployment-Session": self.session, "X-Tiger-Deployment-Lease": self._token,
                 "X-Request-ID": uuid.uuid4().hex})
            received = time.monotonic_ns()
            self.last_attempt.update(response=deepcopy(response), sent_ns=sent, received_ns=received)
            self.v["result"](response)
            self.v["response_rtc"](response.get("rtc"), validated)
            require(response.get("contract_fingerprint") == self.health["acceptance_binding"]["contract_fingerprint"],
                    "Inference model fingerprint changed")
            if self.precision == "bf16":
                require(response.get("policy_precision") == "mixed_bf16_backbone_fp32_rtc", "Inference precision changed")
            self.check_identity()
            finished = time.monotonic_ns()
            cancelled = self._cancelled.is_set() or epoch != self.cancel_epoch or self.failure is not None
            from .window_contract import MODEL_BUDGET_NS
            deadline = capture.anchor_ns + MODEL_BUDGET_NS
            result = {"response": response, "rtc": deepcopy(rtc), "observation_mode": observation_mode,
                    "observation_sha256": capture.snapshot_sha256, "payload_sha256": digest(payload),
                    "model_identity_sha256": self.identity, "observation_anchor_ns": capture.anchor_ns,
                    "request_started_ns": started, "sent_ns": sent, "received_ns": received,
                    "validated_ns": finished, "round_trip_ms": (received - sent) / 1e6,
                    "observation_to_validated_ms": (finished - capture.anchor_ns) / 1e6,
                    "deadline_met": finished <= deadline, "cancelled_result": cancelled,
                    "eligible_for_diagnostic_commit": observation_mode == FRESH and not cancelled and finished <= deadline,
                    "physical_execution_admissible": False, "robot_commands": 0,
                    "evidence_scope": "REPLAYED_REAL_OBSERVATION_NOT_LIVE_ADMISSIBLE" if observation_mode == FROZEN
                                      else "REAL_MODEL_UNSENT_DIAGNOSTIC_REFERENCE"}
            self.last_attempt = {"status": "COMPLETE", **deepcopy(result)}
            return result
        except BaseException as exc:
            self.last_attempt.update(status="FAILED", error_type=type(exc).__name__,
                                     failed_ns=time.monotonic_ns())
            raise
        finally:
            self._flight.release()

    def close(self, timeout=15.0):
        """Cancel then drain before lease release. A drain timeout retains lease."""
        self.cancel()
        if self.session is None:
            return {"released": False, "reason": "no lease acquired", "drained": True}
        require(0 < timeout <= 60, "Bounded drain timeout required")
        deadline = time.monotonic() + timeout
        while True:
            status = self.http.request("GET", "/status")
            require(status.get("selected_step") == self.step, "Model changed during drain")
            require(self._service_instance(status) == self.instance, "Model instance changed during drain")
            if self.precision == "bf16":
                require(status.get("service_nonce") == self.nonce, "Service changed during drain")
            if not self._flight.locked() and status.get("busy") is False:
                break
            require(time.monotonic() < deadline, "Inference did not drain; lease heartbeat retained")
            time.sleep(min(.05, max(0.0, deadline - time.monotonic())))
        with self._lease_io:
            absent = status.get("lease", {}).get("active") is False
            if absent:
                # Expiration or an earlier release whose response was lost.
                # Same instance and both local/server idle were proven above.
                response = {"released": False, "lease_absent_confirmed": True,
                            "reason": "same service idle; lease already absent"}
            else:
                response = self.http.request("POST", "/lease/release",
                                             {"session_id": self.session, "lease_token": self._token})
                require(response.get("released", response.get("ok")) is True, "Lease release unconfirmed")
            self._heartbeat_stop.set()
        if self._worker is not None:
            self._worker.join(3.5)
            require(not self._worker.is_alive(), "Heartbeat did not stop")
        self.session = self._token = None
        return {"released": not absent, "lease_absent_confirmed": absent, "drained": True, "credential_saved": False,
                "response": public_receipt(response)}
