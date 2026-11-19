"""Injected synthetic protocol tests; no real images/model/GPU/network calls."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from service import acceptance as a
from service import contract
from service.server import BF16Service


def config():
    return json.loads((ROOT/"service_config.json").read_text(encoding="utf-8"))


def synthetic_source():
    old = ROOT.parent/"2026-10-20-13-15-BF16独立离线验证/evidence"
    manifest = json.loads((old/"dataset__dataset-manifest.json").read_text(encoding="utf-8"))
    samples = []
    for meta in manifest["samples"]:
        enabled = meta["inference_phase"] == "RTC_CONTINUATION"
        rtc = {"schema": contract.RTC_SCHEMA, "enabled": enabled, "previous_actions": [[0.]*7 for _ in range(35)] if enabled else [],
               "previous_valid_steps": 35 if enabled else 0, "freeze_steps": 12, "execution_label_hz": 18,
               "native_training_hz": 30, "beta": 5., "trial_index": meta["historical_trial_index"],
               "request_index": meta["historical_request_index"]}
        payload = {"state": [0.]*7, "prompt": "SYNTHETIC_PROTOCOL_FIXTURE_NOT_REAL_INPUT",
                   "front_jpeg_base64": "SYNTHETIC_NO_JPEG", "right_jpeg_base64": "SYNTHETIC_NO_JPEG", "rtc": rtc}
        samples.append(a.HistoricalSample(deepcopy(meta), payload,
            {"data_kind": "SYNTHETIC_TEST_ONLY_NOT_HASH_VERIFIED_IMAGES"}, np.zeros((50, 7), np.float32),
            {"data_kind": "SYNTHETIC_ARRAY_NOT_ORIGINAL_BF16_GOLDEN"}))
    return a.HistoricalSource(a.DATASET_SHA, manifest["model"], samples,
        {"data_kind": "SYNTHETIC_FIXTURE_WITH_REAL_METADATA_IDS_ONLY"}, synthetic_fixture=True)


class FakeBackend:
    synthetic_fixture = True
    def __init__(self, source):
        self.source = source
        self.metadata = {**source.model_identity, "precision": contract.POLICY_PRECISION,
                         "precision_audit_passed": True}
        self.rtc = {"schema": contract.RTC_SCHEMA, "previous_valid_steps": 35, "freeze_steps": 12,
                    "execution_label_hz": 18, "native_training_hz": 30, "beta": 5., "precision_audit_passed": True}
        self.audit = {"status": "MIXED_FORWARD_IR_VERIFIED_NOT_QUALITY_ACCEPTANCE", "bf16_dot_conv_nodes": 1,
                      "stablehlo_bf16_dot_conv_nodes": 1, "fp16_nodes": 0, "output_dtypes": ["float32"], "synthetic_test_only": True}
        self.identity = {"source_commit": self.metadata["source_commit"],
                         "normalization_sha256": self.metadata["normalization_sha256"],
                         "tokenizer_sha256": self.metadata["tokenizer_sha256"]}
        self.warmup = {"completed": False, "synthetic_test_only": True}

    def infer_rtc(self, observation):
        request = observation["rtc"]
        sample = next(s for s in self.source.samples if s.payload["rtc"]["request_index"] == request["request_index"])
        # Match the actual frozen MixedPolicy return schema: self.rtc capacity35
        # is static even for native enabled=False/actual known-prefix0 requests.
        returned = {**self.rtc, "enabled": request["enabled"], "trial_index": request["trial_index"],
                    "request_index": request["request_index"], "trial_seed": 42+request["trial_index"],
                    "previous_actions_sha256": a.digest(request["previous_actions"]),
                    "unknown_error_weights_zero": True, "prefix_exactly_overwritten": False,
                    "latent_extra_dimensions_forced_zero": False}
        return {"actions": sample.golden_actions.copy(), "rtc": returned}


class Validators:
    def __init__(self):
        frozen = contract.load_validators()
        self.validate_rtc = frozen.validate_rtc
        self.encode_result = frozen.encode_result
        self.json_safe = frozen.json_safe
    @staticmethod
    def decode_observation(payload):
        if payload["front_jpeg_base64"] != "SYNTHETIC_NO_JPEG":
            raise AssertionError("unit test cannot decode or synthesize real images")
        return deepcopy(payload)


class InMemoryTransport:
    def __init__(self, source):
        self.calls = [];self.after_infer = None;self.fail_route = None
        self.backend = FakeBackend(source)
        self.api = BF16Service(self.backend, 5000, Validators(), service_nonce="a"*32, config_sha256="b"*64)
    def request(self, method, path, payload=None, headers=None):
        self.calls.append((method, path))
        if self.fail_route == path:raise TimeoutError("synthetic route timeout")
        if path == "/health":return self.api.health()
        if path == "/lease/acquire":return self.api.acquire(payload["session_id"], payload["step"], payload["trial_index"], service_nonce=headers[contract.SERVICE_NONCE_HEADER])
        if path == "/lease/heartbeat":return self.api.heartbeat(payload["session_id"], payload["lease_token"])
        if path == "/lease/release":return self.api.release(payload["session_id"], payload["lease_token"])
        if path == "/infer_rtc":
            value = self.api.infer(payload, headers[a.SESSION_HEADER], headers[a.TOKEN_HEADER])
            return self.after_infer(value) if self.after_infer else value
        raise AssertionError("unapproved test route")


def empty_receipt():
    return {"run_id": "c"*32, "records": [], "lease_cycles": [], "historical_service_validated": False}


class TestAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = []
        def forbidden(*args, **kwargs):raise AssertionError("unit tests forbid external operations")
        for owner, name in ((socket, "create_connection"), (socket.socket, "connect"),
                            (subprocess, "Popen"), (subprocess, "run"), (urllib.request, "urlopen")):
            p = patch.object(owner, name, forbidden);p.start();cls.patches.append(p)
    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.patches):p.stop()

    def exercise(self, transport=None, source=None, duration_ms=1):
        source = source or synthetic_source();transport = transport or InMemoryTransport(source)
        receipt = empty_receipt();ticks = iter(range(0, 1000000000000, int(duration_ms*1000000)))
        a.exercise(config(), source, transport, receipt, clock_ns=lambda:next(ticks), heartbeat=False)
        return receipt, transport

    def test_real_saved_16_32_metadata_join_without_arrays_or_raw(self):
        old = ROOT.parent/"2026-10-20-13-15-BF16独立离线验证/evidence"
        manifest = json.loads((old/"dataset__dataset-manifest.json").read_text(encoding="utf-8"))
        results = json.loads((old/"outputs__bf16__results.json").read_text(encoding="utf-8"))
        self.assertEqual(a.digest(results), config()["acceptance"]["golden_results_canonical_sha256"])
        joined = a.validate_metadata_join(manifest, results)
        self.assertEqual(len(joined), 16);self.assertEqual(len(results["records"]), 32)
        bad = deepcopy(results);bad["records"][0]["trial_index"] += 1
        with self.assertRaises(a.AcceptanceError):a.validate_metadata_join(manifest, bad)

    def test_103_repeated_history_calls_8_leases_preserve_rng_no_live_pass(self):
        receipt, transport = self.exercise()
        self.assertEqual(receipt["status"], "SYNTHETIC_PROTOCOL_TEST_ONLY")
        self.assertFalse(receipt["historical_service_validated"])
        self.assertEqual(len(receipt["records"]), 103);self.assertEqual(len(receipt["lease_cycles"]), 8)
        self.assertEqual([r["sequence"] for r in receipt["records"]], list(range(103)))
        self.assertEqual(receipt["records"][0]["rtc"]["previous_valid_steps"], 0)
        self.assertEqual(receipt["records"][3]["historical_rng"]["request_index"], 0)
        self.assertEqual(receipt["records"][19]["historical_rng"]["request_index"], 0)
        self.assertTrue(all(r["golden_F32_bytes_equal_including_signed_zero"] for r in receipt["records"]))
        self.assertEqual(sum(path == "/infer_rtc" for _, path in transport.calls), 103)
        self.assertNotIn("lease_token", json.dumps(receipt))
        self.assertFalse(any(path in ("/select", "/acceptance/submit") for _, path in transport.calls))

    def test_native_capacity35_is_original_response_not_known_prefix(self):
        source = synthetic_source();transport = InMemoryTransport(source)
        lease = a.Lease(transport, "synthetic", 46, transport.api.fingerprint, service_nonce="a"*32, heartbeat=False)
        lease.acquire()
        try:
            sample = source.samples[0]
            response = transport.request("POST", "/infer_rtc", sample.payload, lease.headers())
            self.assertEqual(sample.payload["rtc"]["previous_valid_steps"], 0)
            self.assertEqual(response["rtc"]["previous_valid_steps"], 35)
            self.assertNotIn("latent_dimensions", response["rtc"])
            a.validate_prediction(response, sample.payload, source, transport.api.fingerprint, sample)
        finally:lease.close()

    def test_wrong_owner_nonce_rejected_before_acquire(self):
        source = synthetic_source();transport = InMemoryTransport(source);receipt = empty_receipt()
        with self.assertRaises(a.AcceptanceError):
            a.exercise(config(), source, transport, receipt, heartbeat=False,
                       owner_binding={"nonce": "d"*32, "config_sha256": "b"*64})
        self.assertFalse(any(path == "/lease/acquire" for _, path in transport.calls))

    def test_timeout_failure_retained_no_retry_or_compensating_calls(self):
        source = synthetic_source();transport = InMemoryTransport(source);transport.fail_route = "/infer_rtc"
        receipt = empty_receipt()
        with self.assertRaises(TimeoutError):a.exercise(config(), source, transport, receipt, heartbeat=False)
        self.assertEqual(len(receipt["records"]), 1);self.assertEqual(len(receipt["lease_cycles"]), 1)
        self.assertEqual(receipt["records"][0]["status"], "FAILED_REQUEST_RETAINED")
        self.assertTrue(receipt["lease_cycles"][0]["release"]["released"])

    def test_owner_replaced_after_health_is_atomically_rejected_at_acquire(self):
        source = synthetic_source();transport = InMemoryTransport(source)
        original_request = transport.request
        def request(method, path, payload=None, headers=None):
            if path == "/lease/acquire":transport.api.service_nonce = "d"*32
            return original_request(method, path, payload, headers)
        transport.request = request;receipt = empty_receipt()
        with self.assertRaises(contract.APIError) as failed:
            a.exercise(config(), source, transport, receipt, heartbeat=False,
                       owner_binding={"nonce":"a"*32,"config_sha256":"b"*64})
        self.assertEqual(failed.exception.code,"SERVICE_IDENTITY_MISMATCH")
        self.assertIsNone(transport.api.lease);self.assertEqual(receipt["records"], [])

    def test_expired_lease_and_heartbeat_failure_never_continue(self):
        source = synthetic_source();transport = InMemoryTransport(source);now = [0.]
        transport.api.clock = lambda:now[0]
        lease = a.Lease(transport,"synthetic-expiry",46,transport.api.fingerprint,service_nonce="a"*32,heartbeat=False)
        lease.acquire();now[0] = 3.
        with self.assertRaises(contract.APIError):transport.request("POST","/infer_rtc",source.samples[0].payload,lease.headers())
        self.assertFalse(lease.close()["released"])
        transport = InMemoryTransport(source)
        lease = a.Lease(transport,"synthetic-heartbeat",46,transport.api.fingerprint,service_nonce="a"*32,heartbeat=False)
        lease.acquire();transport.fail_route = "/lease/heartbeat"
        class NoWait:
            def wait(self,_):return False
            def set(self):pass
        lease.stop = NoWait();lease._heartbeat()
        with self.assertRaises(a.AcceptanceError):lease.check()
        self.assertTrue(lease.close()["heartbeat_failed"])

    def test_float32_signed_zero_is_bitwise_failure_even_zero_numeric_error(self):
        source = synthetic_source();source.samples[0].golden_actions[0, 0] = np.float32(-0.)
        transport = InMemoryTransport(source)
        def alter(value):value["actions"][0][0] = 0.;return value
        transport.after_infer = alter;receipt = empty_receipt()
        with self.assertRaisesRegex(a.AcceptanceError, "bitwise"):
            a.exercise(config(), source, transport, receipt, heartbeat=False)
        self.assertFalse(receipt["records"][0]["golden_F32_bytes_equal_including_signed_zero"])
        self.assertEqual(receipt["records"][0]["joint_max_abs_error_deg"], 0.)

    def test_wrong_dtype_shape_prefix_or_fingerprint_rejected(self):
        mutations = [lambda v:v.update(action_dtype="float64"), lambda v:v.update(action_shape=[49,7]),
                     lambda v:v["rtc"].update(previous_actions_sha256="0"*64),
                     lambda v:v.update(contract_fingerprint="0"*64),
                     lambda v:v["actions"][0].__setitem__(0, True)]
        for mutation in mutations:
            source = synthetic_source();transport = InMemoryTransport(source)
            def alter(value):mutation(value);return value
            transport.after_infer = alter;receipt = empty_receipt()
            with self.assertRaises(a.AcceptanceError):a.exercise(config(), source, transport, receipt, heartbeat=False)
            self.assertEqual(len(receipt["records"]), 1)

    def test_other_step_rejected_before_lease(self):
        source = synthetic_source();transport = InMemoryTransport(source)
        transport.api.base_health["policy"]["checkpoint_step"] = 10000
        with self.assertRaises(a.AcceptanceError):a.exercise(config(), source, transport, empty_receipt(), heartbeat=False)
        self.assertEqual(transport.calls, [("GET", "/health")])

    def test_p95_and_max_are_http_service_thresholds_not_robot_commit(self):
        with self.assertRaisesRegex(a.AcceptanceError, "latency budgets"):
            self.exercise(duration_ms=580)
        with self.assertRaisesRegex(a.AcceptanceError, "not a robot deadline"):
            self.exercise(duration_ms=601)

    def test_lease_release_failure_never_passes(self):
        source = synthetic_source();transport = InMemoryTransport(source);transport.fail_route = "/lease/release"
        receipt = empty_receipt()
        with self.assertRaisesRegex(a.AcceptanceError, "cleanup"):
            a.exercise(config(), source, transport, receipt, heartbeat=False)
        self.assertEqual(len(receipt["records"]), 3);self.assertTrue(receipt["lease_cycles"][0]["cleanup_failed"])

    def test_dry_run_creates_nothing_and_never_loads_data_or_http(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/"does-not-exist"
            with patch.object(a, "load_real_source") as data, patch.object(a, "HTTPTransport") as http:
                result = a.run(config(), target, execute_online=False)
            self.assertFalse(target.exists());data.assert_not_called();http.assert_not_called()
            self.assertEqual(result["status"], "DRY_RUN_NOT_EXECUTED")

    def test_original_data_loader_sha_failure_precedes_data_import(self):
        with patch.object(a, "file_sha", return_value="0"*64), patch.object(a, "_load_module") as load:
            with self.assertRaises(a.AcceptanceError):a.load_real_source(config())
            load.assert_not_called()

    def test_full_owner_binding_required_partial_or_changed_owner_rejected(self):
        cfg = config();parent = Path(cfg["remote_root"])/"runs/synthetic"
        identity = {"host": cfg["host"], "boot_id": "synthetic", "pid": 123, "start_ticks": 7,
                    "cmdline_sha256": "a"*64, "identity_kind": "LinuxProcV1"}
        shared = {"nonce": "a"*32, "entrypoint": str(Path(cfg["remote_root"])/"service/server.py"),
                  "run_dir": str(parent), "config_sha256": "b"*64, "step": 5000, "identity": identity}
        launch = {"schema": "tiger-bf16-controller-launch-v1", **shared}
        owner = {"schema": a.SERVICE_SCHEMA, "status": "READY", **shared}
        self.assertEqual(a.validate_owner_records(cfg,parent,"b"*64,launch,owner)["nonce"], "a"*32)
        for changed in ({**owner, "nonce":"c"*32}, {**owner,"status":"LOADING"}, {**owner,"step":10000}):
            with self.assertRaises(a.AcceptanceError):a.validate_owner_records(cfg,parent,"b"*64,launch,changed)


class FakeTimer:
    def __init__(self,*args):pass
    def start(self):pass
    def cancel(self):pass
    def join(self,*args):pass


class TestHTTPBounds(unittest.TestCase):
    def test_monotonic_whole_call_bound_stops_slow_drip(self):
        now = [0.];reads = []
        class Sock:
            def settimeout(self,*args):pass
        class Response:
            status = 200
            def getheader(self,*args):return "application/json"
            def read1(self,n):reads.append(n);now[0] += 1.;return b" "
        class Connection:
            sock = Sock()
            def __init__(self,*args,**kwargs):pass
            def request(self,*args,**kwargs):pass
            def getresponse(self):return Response()
            def close(self):pass
        with patch.object(a.http.client,"HTTPConnection",Connection),patch.object(a.threading,"Timer",FakeTimer),patch.object(a.time,"monotonic",side_effect=lambda:now[0]):
            with self.assertRaisesRegex(a.AcceptanceError,"whole-call"):
                a.HTTPTransport(config()).request("GET","/health")
        self.assertEqual(len(reads),3)

    def test_duplicate_json_and_response_size_bound(self):
        a.strict_loads(b'{"one":1}')
        with self.assertRaises(a.AcceptanceError):a.strict_loads(b'{"one":1,"one":2}')
        with self.assertRaises(a.AcceptanceError):a.strict_loads(b'{"bad":NaN}')
        payload = b"a"*(a.MAX_RESPONSE+1)
        class Response:
            status = 200
            def __init__(self):self.stream = io.BytesIO(payload)
            def getheader(self,*args):return "application/json"
            def read1(self,n):return self.stream.read(n)
        class Connection:
            sock = None
            def __init__(self,*args,**kwargs):pass
            def request(self,*args,**kwargs):pass
            def getresponse(self):return Response()
            def close(self):pass
        with patch.object(a.http.client,"HTTPConnection",Connection),patch.object(a.threading,"Timer",FakeTimer):
            with self.assertRaisesRegex(a.AcceptanceError,"exceeds bound"):
                a.HTTPTransport(config()).request("GET","/health")


if __name__ == "__main__":unittest.main()
