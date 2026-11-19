"""Historical-input HTTP service acceptance; default is a no-network dry run.

This is NOT real NO_SEND, a robot execution, new-feedback RTC, numerical-quality
acceptance or the FP32 manager's acceptance route. No image or credential is
copied into the new receipt. Real dataset loading reuses the frozen 13-15 CPU
loader and its pinned manifest/JPEG/source checks before any service request.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import http.client
import importlib.util
from io import BytesIO
import json
import math
import os
import re
import socket
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if __package__:
    from . import contract
else:
    sys.path.insert(0, str(ROOT))
    from service import contract
SCHEMA = "tiger-bf16-historical-http-acceptance-v1"
SCOPE = "REPEATED_REAL_HISTORICAL_INPUT_SERVICE_TEST_NOT_NO_SEND_OR_NEW_CLOSED_LOOP"
SERVICE_SCHEMA = "tiger-bf16-rtc-service-v1"
POLICY_PRECISION = "mixed_bf16_backbone_fp32_rtc"
SESSION_HEADER = "X-Tiger-Deployment-Session"
TOKEN_HEADER = "X-Tiger-Deployment-Lease"
DATASET_SHA = "5b180d9c8342923dcb23e5e74c4b3654ec121d203835561a366b3efc0fc168ca"
LOADER_SHA = "649856f98ff0c7f82e7201dd089b7444287f5003d819436495dda257576cd974"
MAX_RESPONSE = 4*1024*1024


class AcceptanceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strict_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON response field")
            result[key] = value
        return result
    def bad(_):
        raise AcceptanceError("nonfinite JSON response")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+".writing")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary, path)


def validate_config(config):
    require(isinstance(config, dict) and config.get("schema") == "tiger-bf16-rtc-service-config-v1", "wrong independent service config")
    require(config.get("bind") == "127.0.0.1" and type(config.get("port")) is int and config["port"] == 18960,
            "only the independent loopback 127.0.0.1:18960 is supported")
    a = config.get("acceptance", {})
    for key, value in (("warmup_calls", 3), ("measured_calls", 100), ("p95_budget_ms", 550), ("max_budget_ms", 600)):
        require(type(a.get(key)) is int and a[key] == value, "frozen acceptance criterion differs: "+key)
    require(a.get("no_quality_or_motion_acceptance") is True, "scope must exclude quality and motion acceptance")
    require(isinstance(config.get("historical_dataset_root"), str) and config["historical_dataset_root"], "explicit real historical dataset path required")
    require(a.get("golden_required") is True and isinstance(a.get("golden_results_path"), str), "strict BF16 golden arrays are required; no summary-only fallback")
    require(isinstance(a.get("golden_results_canonical_sha256"), str) and len(a["golden_results_canonical_sha256"]) == 64,
            "independent pinned BF16 golden metadata SHA required")
    require(config.get("acceptance_step", 5000) == 5000, "saved historical/golden data only supports step5000")
    return deepcopy(config)


@dataclass
class HistoricalSample:
    metadata: dict
    payload: dict
    source_metadata: dict
    golden_actions: object
    golden_metadata: dict


@dataclass
class HistoricalSource:
    manifest_sha256: str
    model_identity: dict
    samples: list
    metadata: dict
    synthetic_fixture: bool = False


def _load_module(path):
    name = "_bf16_acceptance_strict_historical_loader"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_metadata_join(manifest, results):
    """Validate saved 16/32 row metadata without images, arrays or JAX imports."""
    require(manifest.get("schema") == "tiger-real-history-offline-v1" and len(manifest.get("samples", [])) == 16,
            "expected the frozen real16 metadata")
    require(results.get("schema") == "tiger-offline-precision-replay-v1" and results.get("mode") == "bf16"
            and results.get("dataset_manifest_sha256") == DATASET_SHA, "BF16 golden result identity differs")
    require(manifest.get("checkpoint_step") == results.get("checkpoint", {}).get("step") == 5000,
            "metadata/golden only supports checkpoint5000")
    model = manifest["model"]
    for key in ("source_commit", "normalization_sha256", "tokenizer_sha256"):
        require(results["checkpoint"].get(key) == model[key], "golden model identity differs: "+key)
    goldens = {}
    for row in results.get("records", []):
        key = (row.get("sample_id"), row.get("seed_id"))
        require(key not in goldens, "duplicate BF16 golden sample/seed")
        goldens[key] = row
    selected, seen = {}, set()
    for meta in manifest["samples"]:
        require(meta["sample_id"] not in seen, "duplicate historical sample metadata")
        seen.add(meta["sample_id"])
        expected = goldens.get((meta["sample_id"], "historical"))
        require(isinstance(expected, dict), "BF16 historical golden missing")
        for key, value in (("source_input_sha256", meta["input_sha256"]),
                           ("trial_index", meta["historical_trial_index"]),
                           ("request_index", meta["historical_request_index"]),
                           ("inference_phase", meta["inference_phase"])):
            require(expected.get(key) == value, "BF16 golden seed/context differs: "+key)
        selected[meta["sample_id"]] = deepcopy(expected)
    return selected


def load_real_source(config):
    """CPU-only verification. Does not instantiate a policy or call a service."""
    import numpy as np
    loader_path = ROOT/"frozen/offline_dataset.py"
    require(file_sha(loader_path) == LOADER_SHA, "frozen offline_dataset implementation differs")
    loader = _load_module(loader_path)
    dataset = Path(config["historical_dataset_root"]).resolve(strict=True)
    manifest, manifest_sha, decoded = loader.load_dataset(dataset)
    require(manifest_sha == DATASET_SHA and manifest["checkpoint_step"] == 5000, "real16 dataset identity differs")
    # load_dataset independently validates every input/response/JPEG and the old
    # experiment's three frozen sources. Re-read the original JPEG wire only;
    # never re-encode decoded RGB or create a replacement image.
    golden_path = Path(config["acceptance"]["golden_results_path"]).resolve(strict=True)
    results = strict_loads(golden_path.read_bytes())
    require(digest(results) == config["acceptance"]["golden_results_canonical_sha256"], "BF16 golden metadata SHA differs from independently pinned config")
    model = manifest["model"]
    goldens = validate_metadata_join(manifest, results)
    samples = []
    for meta, observation, _old_fp32_response in decoded:
        source_path = (dataset/meta["input_file"]).resolve(strict=True);source_path.relative_to(dataset)
        original_input = source_path.read_bytes()
        require(hashlib.sha256(original_input).hexdigest() == meta["input_sha256"], "historical input changed after strict loading")
        saved = strict_loads(original_input)
        snapshot = saved["snapshot"]
        payload = {"state": deepcopy(snapshot["state_right_rad_m"]),
                   "front_jpeg_base64": snapshot["frames"]["front"]["jpeg_base64"],
                   "right_jpeg_base64": snapshot["frames"]["right"]["jpeg_base64"],
                   "prompt": saved["prompt"], "rtc": deepcopy(saved["rtc"])}
        require(payload["rtc"] == observation["rtc"], "wire and strict-decoded RTC differ")
        expected = goldens.get(meta["sample_id"])
        require(isinstance(expected, dict), "BF16 historical golden missing")
        for key, value in (("source_input_sha256", meta["input_sha256"]),
                           ("trial_index", payload["rtc"]["trial_index"]),
                           ("request_index", payload["rtc"]["request_index"]),
                           ("inference_phase", meta["inference_phase"])):
            require(expected.get(key) == value, "BF16 golden seed/context differs: "+key)
        array_path = (golden_path.parent/expected["actions_file"]).resolve(strict=True)
        array_path.relative_to(golden_path.parent)
        original_array = array_path.read_bytes()
        require(hashlib.sha256(original_array).hexdigest() == expected["actions_sha256"], "BF16 golden .npy SHA differs")
        golden = np.load(BytesIO(original_array), allow_pickle=False)
        require(isinstance(golden, np.ndarray) and golden.dtype == np.dtype("float32")
                and golden.shape == (50, 7) and np.isfinite(golden).all(), "golden must be finite F32[50,7]")
        source_metadata = {"input_file": str(source_path), "input_sha256": meta["input_sha256"],
                           "request_payload_sha256": digest(payload), "source_request_id": saved["request_id"],
                           "jpeg_sha256": {role: snapshot["frames"][role]["sha256"] for role in ("front", "right")},
                           "original_hardware_synchronized": snapshot["hardware_synchronized"]}
        samples.append(HistoricalSample(deepcopy(meta), payload, source_metadata, golden.copy(),
            {"path": str(array_path), "sha256": expected["actions_sha256"], "seed_id": "historical"}))
    require(len(samples) == 16, "strict historical sample count differs")
    return HistoricalSource(manifest_sha, deepcopy(model), samples,
        {"dataset_root": str(dataset), "golden_results_path": str(golden_path),
         "golden_results_sha256": file_sha(golden_path), "strict_loader_sha256": file_sha(loader_path),
         "data_kind": "REAL_HISTORICAL_HASH_VERIFIED_NOT_LIVE_OBSERVATIONS"})


class HTTPTransport:
    """Fresh bounded loopback connection per request; no retries or redirects."""
    def __init__(self, config):
        self.host, self.port = "127.0.0.1", 18960
        require(config["bind"] == self.host and config["port"] == self.port, "wrong endpoint")

    def request(self, method, path, payload=None, headers=None):
        require(path in ("/health", "/infer_rtc", "/lease/acquire", "/lease/heartbeat", "/lease/release"), "acceptance route not permitted")
        deadline = time.monotonic()+3.
        body = None if payload is None else canonical(payload)
        require(body is None or len(body) <= contract.MAX_BODY_BYTES, "historical HTTP input exceeds original bounded body size")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3.)
        expired, sockets = threading.Event(), []
        def expire():
            expired.set()
            for sock in list(sockets):
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            connection.close()
        watchdog = threading.Timer(3., expire)
        watchdog.daemon = True;watchdog.start()
        def remaining():
            left = deadline-time.monotonic()
            require(left > 0 and not expired.is_set(), "HTTP whole-call deadline exceeded")
            return left
        try:
            merged = {"Accept": "application/json", "Connection": "close", **(headers or {})}
            if body is not None:
                merged.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
            connection.request(method, path, body=body, headers=merged)
            if connection.sock is not None:sockets.append(connection.sock)
            remaining()
            response = connection.getresponse()
            require(response.getheader("Content-Type", "").split(";")[0].strip() == "application/json", "non-JSON HTTP response")
            raw = bytearray()
            while True:
                left = remaining()
                for sock in sockets:sock.settimeout(left)
                part = response.read1(min(65536, MAX_RESPONSE+1-len(raw)))
                remaining()
                if not part:break
                raw.extend(part)
                require(len(raw) <= MAX_RESPONSE, "HTTP response exceeds bound")
            remaining()
            value = strict_loads(raw)
            require(response.status == 200, "service rejected request: HTTP "+str(response.status))
            require(isinstance(value, dict), "service response must be an object")
            remaining();return value
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise AcceptanceError("bounded service transport failed: "+type(exc).__name__) from exc
        finally:
            watchdog.cancel();watchdog.join(.2)
            connection.close()


class Lease:
    """One active lease, independently heartbeated; credentials stay in RAM."""
    def __init__(self, transport, session_id, trial_index, fingerprint, *, service_nonce, heartbeat=True):
        self.transport, self.session, self.trial, self.fingerprint = transport, session_id, trial_index, fingerprint
        require(isinstance(service_nonce, str) and re.fullmatch("[0-9a-f]{32}", service_nonce), "bound service nonce required before acquire")
        self.service_nonce = service_nonce
        self.token = None;self.stop = threading.Event();self.failed = threading.Event();self.failure = None
        self.thread = None;self.heartbeat_enabled = heartbeat

    def acquire(self):
        reply = self.transport.request("POST", "/lease/acquire", {"session_id": self.session, "step": 5000, "trial_index": self.trial},
                                       {contract.SERVICE_NONCE_HEADER: self.service_nonce})
        token = reply.get("lease_token")
        require(isinstance(token, str) and 16 <= len(token) <= 256 and "\n" not in token, "invalid lease credential")
        self.token = token  # Retain known credentials for cleanup if later identity checks fail.
        require(reply.get("session_id") == self.session and reply.get("step") == 5000
                and reply.get("trial_index") == self.trial and reply.get("trial_seed") == 42+self.trial,
                "lease RNG/session identity differs")
        require(reply.get("contract_fingerprint") == self.fingerprint and reply.get("ttl_s") == 2.
                and reply.get("heartbeat_interval_s") == .5, "lease fingerprint/timing contract differs")
        if self.heartbeat_enabled:
            self.thread = threading.Thread(target=self._heartbeat, daemon=True, name="bf16-history-acceptance-lease")
            self.thread.start()
        return {k: reply[k] for k in ("session_id", "step", "trial_index", "trial_seed", "ttl_s", "heartbeat_interval_s", "contract_fingerprint")}

    def _heartbeat(self):
        while not self.stop.wait(.5):
            try:
                reply = self.transport.request("POST", "/lease/heartbeat", {"session_id": self.session, "lease_token": self.token})
                require(reply.get("session_id") == self.session and reply.get("step") == 5000, "heartbeat owner changed")
            except Exception as exc:
                self.failure = "heartbeat failed: "+type(exc).__name__;self.failed.set();return

    def check(self):
        require(self.token is not None and not self.failed.is_set(), self.failure or "no active historical lease")

    def headers(self):
        self.check();return {SESSION_HEADER: self.session, TOKEN_HEADER: self.token}

    def close(self):
        self.stop.set();errors = []
        if self.thread is not None:
            self.thread.join(4.)
            if self.thread.is_alive():errors.append("heartbeat thread did not stop")
        released = False
        if self.token is not None:
            try:
                reply = self.transport.request("POST", "/lease/release", {"session_id": self.session, "lease_token": self.token})
                require(reply.get("released") is True, "service did not confirm lease release")
                released = True
            except Exception as exc:
                errors.append("release failed: "+type(exc).__name__)
            finally:self.token = None
        return {"released": released, "session_id": self.session, "errors": errors,
                "heartbeat_failed": self.failed.is_set(), "credential_saved": False}


def validate_health(health, source, fingerprint=None):
    try:contract.validate_health(health, 5000, allow_synthetic=source.synthetic_fixture)
    except Exception as exc:raise AcceptanceError("independent mixed-BF16 health rejected: "+type(exc).__name__) from exc
    require(health.get("synthetic_fixture") is source.synthetic_fixture, "source/service synthetic scope differs")
    policy = health["policy"]
    for key in ("source_commit", "normalization_sha256", "tokenizer_sha256"):
        require(policy.get(key) == source.model_identity[key], "service/source identity differs: "+key)
    value = health["contract_fingerprint"]
    require(fingerprint is None or value == fingerprint, "service fingerprint changed during acceptance")
    return value


def validate_owner_records(config, service_run, config_sha256, launch, owner):
    """Pure metadata validation; never recovers a partial launch implicitly."""
    require(isinstance(launch, dict) and launch.get("schema") == "tiger-bf16-controller-launch-v1", "complete controller-launch.json required")
    require(isinstance(owner, dict) and owner.get("schema") == SERVICE_SCHEMA and owner.get("status") == "READY", "matching READY owner.json required")
    nonce = launch.get("nonce")
    require(isinstance(nonce, str) and re.fullmatch("[0-9a-f]{32}", nonce), "controller nonce missing")
    require(launch.get("step") == owner.get("step") == 5000, "strict historical acceptance only supports service step5000")
    expected = {"nonce": nonce, "entrypoint": str(Path(config["remote_root"])/"service/server.py"),
                "run_dir": str(service_run), "config_sha256": config_sha256, "step": 5000}
    require(all(launch.get(k) == v and owner.get(k) == v for k, v in expected.items()), "controller/owner/config binding differs")
    identity = launch.get("identity")
    require(isinstance(identity, dict) and identity.get("host") == config["host"]
            and identity.get("identity_kind") == "LinuxProcV1" and type(identity.get("pid")) is int and identity["pid"] > 1
            and type(identity.get("start_ticks")) is int and identity["start_ticks"] >= 0,
            "valid service process identity required")
    require(canonical(owner.get("identity")) == canonical(identity), "controller/owner process identity differs")
    return {**expected, "identity": deepcopy(identity), "config_sha256_basis": "SERVICE_CONFIG_FILE_BYTES_SHA256"}


def read_owned_service(config, config_path, service_run, config_sha256):
    """Online-only host/SSD/source/process metadata guards, no GPU locks/model."""
    from service.control import LinuxRuntime, identity_matches
    runtime = LinuxRuntime(config, config_path)
    environment = runtime.check_environment()
    parent = Path(service_run).resolve(strict=True)
    launch_path, owner_path = parent/"controller-launch.json", parent/"owner.json"
    launch_raw, owner_raw = launch_path.read_bytes(), owner_path.read_bytes()
    launch, owner = strict_loads(launch_raw), strict_loads(owner_raw)
    binding = validate_owner_records(config, parent, config_sha256, launch, owner)
    pid = binding["identity"]["pid"]
    require(identity_matches(runtime.identity(pid), binding["identity"])
            and runtime.command(pid) == launch.get("argv") and runtime.nonce(pid) == binding["nonce"],
            "service owner process changed; preserve it without acquire")
    return {**binding, "controller_launch_sha256": hashlib.sha256(launch_raw).hexdigest(),
            "owner_file_sha256": hashlib.sha256(owner_raw).hexdigest(), "environment": environment}


def validate_prediction(response, payload, source, fingerprint, sample):
    import numpy as np
    require(response.get("policy_precision") == POLICY_PRECISION and response.get("contract_fingerprint") == fingerprint,
            "response BF16 identity differs")
    require(response.get("action_dtype") == "float32" and response.get("action_shape") == [50, 7], "response F32 H50 shape differs")
    values = response.get("actions")
    require(isinstance(values, list) and len(values) == 50 and all(isinstance(row, list) and len(row) == 7
            and all(type(x) in (int, float) and math.isfinite(x) for x in row) for row in values), "finite unmodified H50 actions required")
    with np.errstate(over="ignore", invalid="ignore"):
        array = np.asarray(values, dtype=np.float32)
    require(np.isfinite(array).all() and np.array_equal(np.asarray(values, dtype=np.float64), array.astype(np.float64)),
            "response values are not exact finite F32 wire values")
    rtc = deepcopy(payload["rtc"])
    rtc.update(trial_seed=42+rtc["trial_index"], previous_actions_sha256=digest(rtc["previous_actions"]))
    try:contract.validate_response_rtc(response.get("rtc"), rtc)
    except Exception as exc:raise AcceptanceError("response RTC/RNG/prefix differs") from exc
    golden = sample.golden_actions
    require(getattr(golden, "shape", None) == (50, 7) and golden.dtype == np.dtype("float32"), "strict golden F32 array missing")
    actual_bytes, expected_bytes = array.tobytes(order="C"), golden.tobytes(order="C")
    difference = np.abs(array.astype(np.float64)-golden.astype(np.float64))
    metrics = {"action_array_sha256": hashlib.sha256(actual_bytes).hexdigest(),
               "golden_array_bytes_sha256": hashlib.sha256(expected_bytes).hexdigest(),
               "golden_F32_bytes_equal_including_signed_zero": actual_bytes == expected_bytes,
               "joint_max_abs_error_deg": float(difference[:, :6].max()*180/math.pi),
               "gripper_max_abs_error_mm": float(difference[:, 6].max()*1000),
               "action_dtype": "float32", "action_shape": [50, 7]}
    return metrics


def percentile(values, fraction):
    ordered = sorted(values);p = (len(ordered)-1)*fraction;i = int(p)
    return ordered[i]+(ordered[min(i+1, len(ordered)-1)]-ordered[i])*(p-i)


def exercise(config, source, transport, receipt, *, clock_ns=time.monotonic_ns, heartbeat=True, owner_binding=None):
    """Injected protocol engine. Synthetic sources can NEVER become live PASS."""
    require(source.manifest_sha256 == DATASET_SHA and source.model_identity.get("checkpoint_step") == 5000
            and len(source.samples) == 16, "verified real16 step5000 source contract required")
    for index, sample in enumerate(source.samples):
        require(sample.payload["rtc"]["trial_index"] == sample.metadata["historical_trial_index"]
                and sample.payload["rtc"]["request_index"] == sample.metadata["historical_request_index"], "historical source identity differs")
        if index:
            require(source.samples[index-1].payload["rtc"]["request_index"] < sample.payload["rtc"]["request_index"], "history indices must remain sparse increasing")
    def check_health(fingerprint=None):
        health = transport.request("GET", "/health")
        if owner_binding is not None:
            require(health.get("service_nonce") == owner_binding["nonce"]
                    and health.get("config_sha256") == owner_binding["config_sha256"], "loopback port belongs to a different service owner/config; no lease acquired")
        else:require(source.synthetic_fixture, "real acceptance requires controller/owner nonce binding")
        require(isinstance(health.get("service_nonce"), str) and re.fullmatch("[0-9a-f]{32}", health["service_nonce"]), "health service nonce missing")
        receipt["observed_service_nonce"] = health["service_nonce"]
        return validate_health(health, source, fingerprint)
    fingerprint = check_health()
    receipt.update(service_contract_fingerprint=fingerprint, source_metadata=deepcopy(source.metadata),
                   dataset_manifest_sha256=source.manifest_sha256, synthetic_fixture=source.synthetic_fixture)
    sequence, cycle = 0, 0
    for phase, total in (("warmup", 3), ("measured", 100)):
        offset = 0
        while offset < total:
            group = source.samples[:min(16, total-offset)]
            require(len({sample.payload["rtc"]["trial_index"] for sample in group}) == 1, "a replay cycle needs one historical trial")
            lease = Lease(transport, "bf16-history-"+receipt["run_id"]+"-"+str(cycle), group[0].payload["rtc"]["trial_index"], fingerprint,
                          service_nonce=receipt["observed_service_nonce"], heartbeat=heartbeat)
            cycle_record = {"lease_cycle": cycle, "phase": phase, "planned_calls": len(group), "status": "STARTED"}
            receipt["lease_cycles"].append(cycle_record)
            try:
                check_health(fingerprint)
                cycle_record["acquire"] = lease.acquire()
                for sample in group:
                    lease.check()
                    rtc = deepcopy(sample.payload["rtc"])
                    row = {"sequence": sequence, "phase": phase, "lease_cycle": cycle,
                           "sample_id": sample.metadata["sample_id"], "source": deepcopy(sample.source_metadata),
                           "historical_rng": {"trial_index": rtc["trial_index"], "request_index": rtc["request_index"],
                                              "trial_seed": 42+rtc["trial_index"]},
                           "rtc": rtc, "previous_actions_sha256": digest(rtc["previous_actions"]),
                           "golden": deepcopy(sample.golden_metadata), "status": "REQUEST_STARTED"}
                    receipt["records"].append(row)
                    started = clock_ns();row["client_started_monotonic_ns"] = started
                    try:
                        response = transport.request("POST", "/infer_rtc", sample.payload, lease.headers())
                        lease.check()
                        row.update(validate_prediction(response, sample.payload, source, fingerprint, sample))
                        finished = clock_ns();elapsed = (finished-started)/1e6
                        require(type(finished) is int and finished >= started, "client clock reversed")
                        row.update(client_validated_monotonic_ns=finished, http_input_to_validated_response_ms=elapsed,
                                   within_600ms=elapsed <= 600, status="VALIDATED_HISTORICAL_RESPONSE")
                        require(row["golden_F32_bytes_equal_including_signed_zero"], "golden F32 bitwise mismatch; no numerical threshold fallback")
                        require(elapsed <= 600, "historical HTTP request exceeded600ms; not a robot deadline")
                    except Exception as exc:
                        row.update(status="FAILED_REQUEST_RETAINED", failure_type=type(exc).__name__, failure=str(exc),
                                   client_failed_monotonic_ns=clock_ns())
                        raise
                    sequence += 1
                cycle_record["status"] = "REPLAY_CYCLE_COMPLETED"
            except BaseException as exc:
                cycle_record.update(status="FAILED_REPLAY_CYCLE_RETAINED", failure_type=type(exc).__name__, failure=str(exc))
                raise
            finally:
                cycle_record["release"] = lease.close()
                closed = cycle_record["release"]
                if not (closed["released"] is True and not closed["errors"] and not closed["heartbeat_failed"]):
                    cycle_record["cleanup_failed"] = True
                    # Retain the primary call exception; cleanup never replaces
                    # its failure evidence or permits a new lease to retry it.
                    if cycle_record["status"] != "FAILED_REPLAY_CYCLE_RETAINED":
                        raise AcceptanceError("lease cleanup/heartbeat failed; partial evidence retained")
            cycle += 1;offset += len(group)
    check_health(fingerprint)
    measured = [r["http_input_to_validated_response_ms"] for r in receipt["records"] if r["phase"] == "measured"]
    require(len(measured) == 100 and len(receipt["records"]) == 103, "partial acceptance cannot PASS")
    p95, peak = percentile(measured, .95), max(measured)
    receipt["latency_ms"] = {"n": 100, "p50": percentile(measured, .5), "p95": p95, "max": peak}
    require(p95 <= 550 and peak <= 600, "historical HTTP latency budgets failed")
    receipt.update(status="SYNTHETIC_PROTOCOL_TEST_ONLY" if source.synthetic_fixture else "HISTORICAL_SERVICE_ACCEPTANCE_PASS",
                   historical_service_validated=not source.synthetic_fixture,
                   golden_replay_bitwise_verified=True, completed_warmup=3, completed_measured=100)
    return receipt


def run(config, run_dir, *, execute_online=False, config_path=None, config_sha256=None,
        source_loader=load_real_source, transport_factory=HTTPTransport):
    config = validate_config(config)
    receipt = {"schema": SCHEMA, "run_id": uuid.uuid4().hex, "scope": SCOPE, "status": "DRY_RUN_NOT_EXECUTED",
               "historical_service_validated": False, "config_sha256": config_sha256,
               "config_sha256_basis": "SERVICE_CONFIG_FILE_BYTES_SHA256",
               "source_implementation_sha256": file_sha(Path(__file__)), "records": [], "lease_cycles": [],
               "robot_NO_SEND_validated": False, "new_closed_loop_validated": False, "quality_or_motion_accepted": False,
               "manager_acceptance_written": False, "images_copied": False, "planned_warmup": 3, "planned_measured": 100,
               "timing_basis": "HTTP_HISTORICAL_REQUEST_TO_VALIDATED_RESPONSE_NOT_ROBOT_COMMIT"}
    if not execute_online:
        return {**receipt, "attempted_calls": 0, "service_run_dir": str(run_dir), "writes_performed": False,
                "network_requests_performed": False, "source_validation": "NOT_EXECUTED_IN_DRY_RUN"}
    require(isinstance(config_sha256, str) and re.fullmatch("[0-9a-f]{64}", config_sha256)
            and config_path is not None and file_sha(config_path) == config_sha256,
            "exact source service_config.json file-byte SHA required")
    service_run = Path(run_dir).resolve(strict=True)
    binding = read_owned_service(config, config_path, service_run, config_sha256)
    run_dir = service_run/(time.strftime("acceptance-%Y-%m-%d-%H-%M-%S-")+uuid.uuid4().hex[:10])
    run_dir.mkdir(exist_ok=False)
    receipt.update(service_owner_binding=binding, service_run_dir=str(service_run),
                   run_dir=str(run_dir), receipt=str(run_dir/"historical-service-acceptance.json"))
    try:
        source = source_loader(config)  # All original hashes/arrays before first HTTP.
        receipt["status"] = "STARTED_HISTORICAL_SERVICE_ACCEPTANCE"
        exercise(config, source, transport_factory(config), receipt, owner_binding=binding)
    except BaseException as exc:
        receipt.update(status="PARTIAL_OR_FAILED_NOT_ACCEPTED", failure_type=type(exc).__name__, failure=str(exc),
                       completed_warmup=sum(r["phase"] == "warmup" and r["status"] == "VALIDATED_HISTORICAL_RESPONSE" for r in receipt["records"]),
                       completed_measured=sum(r["phase"] == "measured" and r["status"] == "VALIDATED_HISTORICAL_RESPONSE" for r in receipt["records"]))
    finally:
        receipt["attempted_calls"] = len(receipt["records"])
        write_json(run_dir/"historical-service-acceptance.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--execute-online", action="store_true")
    args = parser.parse_args()
    raw_config = args.config.read_bytes();config = strict_loads(raw_config)
    result = run(config, args.run_dir, execute_online=args.execute_online, config_path=args.config,
                 config_sha256=hashlib.sha256(raw_config).hexdigest())
    print(json.dumps({"status": result["status"], "receipt": result.get("receipt"),
                      "attempted_calls": result["attempted_calls"], "scope": SCOPE}))
    if args.execute_online and result["status"] != "HISTORICAL_SERVICE_ACCEPTANCE_PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
