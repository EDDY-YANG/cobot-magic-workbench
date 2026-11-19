"""Optional StartLux side-channel observer; never a robot command or safety gate.

Owner: StartLux local deployment task, 2026-10-18. Only the caller supplies
images/state, from an existing producer or an explicitly selected replay. This
module does not open cameras, CAN, SSH, models, datasets, or log files.

Snapshot protocol: source (live/replay), task_id, session_id, request_id,
frame_id, stamps (opaque original clock metadata), state (JSON), images
(base64/data-URI strings), and optional image_roles. Source clock values are
never subtracted from this machine's monotonic clock. In particular, a recent
replay inference is not a fresh physical observation.
"""
from collections import deque
from copy import deepcopy
import base64
import io
import json
import math
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SCHEMA = "tiger-semantic-observer-v1"
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
from .temporal_semantic import PHASE_PROFILES, PHASE_QUESTION, phase_payload

INPUT_PROFILES = ("full_provenance_v1", "compact_replay_v1") + PHASE_PROFILES + ('focused_dual_history_v2',)
QUESTIONS = {
    "phase": {
        "type": "choice",
        "instructions": "From the supplied evidence only, which observable phase best describes the right arm "
                        "moving a red block to a green bucket? A single image cannot establish motion. "
                        "Use unknown when the phase or object identity cannot be observed reliably.",
        "criteria": {
            "approaching": "Visible approach/alignment evidence near the red block, before grasping",
            "grasping": "Visible gripper contact or closing around the red block",
            "transporting": "Visible evidence of the red block held while moving toward the bucket",
            "placing": "Visible evidence of lowering/releasing the red block at the green bucket",
            "returning": "Visible evidence of an empty gripper returning after a release",
            "unknown": "Insufficient, ambiguous, occluded, out-of-task or contradictory evidence",
        },
    },
    "holding": {
        "type": "choice",
        "instructions": "Does the supplied visual evidence show the right gripper holding the red block? "
                        "Do not infer holding from gripper closure or task instructions alone.",
        "criteria": {
            "held": "Red block visibly held by the right gripper",
            "not_held": "Right gripper visibly not holding the red block",
            "unknown": "Cannot verify object identity, contact or holding from this evidence",
        },
    },
    "placement": {
        "type": "choice",
        "instructions": "Does the supplied visual evidence show the red block released inside the green bucket? "
                        "This is a visual candidate, never proof of a completed physical task.",
        "criteria": {
            "in_bucket": "Red block visibly inside the green bucket and visibly released from the gripper",
            "not_in_bucket": "Red block visibly outside the green bucket or still visibly held",
            "unknown": "Occluded, ambiguous, missing or insufficient evidence of release/containment",
        },
    },
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(name + " must be a non-empty string of at most 256 characters")
    return value


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def validate_snapshot(value):
    """Freeze a caller-owned snapshot; validate bounds without inventing evidence."""
    if not isinstance(value, dict) or value.get("source") not in ("live", "replay"):
        raise ValueError("source must be live or replay")
    frozen = {"source": value["source"]}
    for key in ("task_id", "session_id", "request_id", "frame_id"):
        frozen[key] = _text(value.get(key), key)
    if not isinstance(value.get("stamps"), dict):
        raise ValueError("stamps must preserve the original source clock metadata as a JSON object")
    frozen["stamps"] = deepcopy(value["stamps"])
    if len(_json_bytes(frozen["stamps"])) > 65536:
        raise ValueError("stamps metadata exceeds 64 KiB")
    if "state" not in value:
        raise ValueError("state is required, including explicit null when there is no textual evidence")
    frozen["state"] = deepcopy(value["state"])
    images = value.get("images", [])
    if not isinstance(images, list) or len(images) > 8 or any(
            not isinstance(item, str) or not item.strip() for item in images):
        raise ValueError("images must contain at most eight non-empty base64/data-URI strings")
    frozen["images"] = list(images)
    roles = value.get("image_roles", [])
    if not isinstance(roles, list) or (roles and len(roles) != len(images)) or any(
            not isinstance(role, str) or not role.strip() or len(role) > 128 for role in roles):
        raise ValueError("image_roles must be empty or identify every supplied image")
    frozen["image_roles"] = list(roles)
    if len(_json_bytes(frozen)) > MAX_REQUEST_BYTES - 16384:
        raise ValueError("snapshot exceeds the bounded HTTP request size")
    return frozen


def _identity(snapshot):
    return {key: deepcopy(snapshot[key]) for key in (
        "source", "task_id", "session_id", "request_id", "frame_id", "stamps", "image_roles")}


def _no_old_labels(value):
    if isinstance(value, dict):
        if {"verdict", "human_result", "human_label", "success", "stage_observer", "perception",
                "phase", "action_targets", "action"}.intersection(value):
            raise ValueError("Human outcomes, old labels and actions are not compact input evidence")
        for item in value.values():
            _no_old_labels(item)
    elif isinstance(value, list):
        for item in value:
            _no_old_labels(item)


def _compact_replay_state(snapshot):
    """The benchmark's compact_3q state, with unchanged original JPEG data URIs.

    UUIDs/absolute clocks stay in the local result identity. Relative age is
    calculated only within the original verified recorded clock domain. Pillow
    is imported only for this explicit profile and never re-encodes an image.
    """
    if snapshot["source"] != "replay":
        raise ValueError("compact_replay_v1 accepts reviewed historical replay only")
    _no_old_labels(snapshot["state"])
    if snapshot["image_roles"] != ["front", "right"] or len(snapshot["images"]) != 2:
        raise ValueError("compact_replay_v1 requires the two original front/right images in order")
    actual = snapshot["state"]["right"]
    index = snapshot["stamps"]["camera_source_index_v1"]
    domain, sample_mono = index["clock_domain"], index["sample_monotonic_ns"]
    if not isinstance(domain, dict) or domain.get("identity_verified") is not True or type(sample_mono) is not int:
        raise ValueError("Recorded source clock must already be verified")
    from PIL import Image
    quality = []
    for role, value in zip(snapshot["image_roles"], snapshot["images"]):
        raw = base64.b64decode(value.split(",", 1)[1] if value.startswith("data:") else value, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            size = list(image.size)
        timing = index["cameras"][role]["source_timing"]
        source_mono = timing["source_monotonic_ns_estimate"]
        if timing.get("clock_domain") != domain or type(source_mono) is not int or not 0 <= source_mono <= sample_mono:
            raise ValueError("Only compare clocks inside the original verified domain")
        quality.append({"role": role, "size_px": size,
                        "age_ms_at_recorded_sample": (sample_mono - source_mono) / 1e6})
    q, grip = actual["q_deg"], actual["grip_m"]
    if not isinstance(q, list) or len(q) != 6 or not all(_finite(v) for v in q) or not _finite(grip):
        raise ValueError("Require actual finite right-arm feedback")
    offset = snapshot["stamps"]["offset_s"]
    if not _finite(offset) or offset < 0:
        raise ValueError("Require the original episode-relative offset")
    return {"task": snapshot["task_id"], "source": "historical_replay",
            "relative_time_s": offset, "image_order": list(snapshot["image_roles"]),
            "right_actual": {key: deepcopy(actual[key]) for key in
                ("q_deg", "grip_m", "feedback_fresh", "age_joint_s", "age_gripper_s", "age_status_s") if key in actual},
            "image_quality_at_recording": quality,
            "hardware_exposure_timestamp_available": snapshot["stamps"].get("hardware_exposure_timestamp_available"),
            "limitations": "Single recorded observation. Software source timestamps do not certify physical success or current live freshness."}


def _request_input(payload, profile):
    state_chars = len(_json_bytes(payload["state"]).decode("utf-8"))
    question_chars = len(_json_bytes(payload["questions"]).decode("utf-8"))
    return {"input_profile": profile, "payload_state_json_chars": state_chars,
            "payload_questions_json_chars": question_chars, "payload_text_json_chars": state_chars + question_chars,
            "request_image_count": len(payload["images"]),
            "character_count_definition": "State JSON plus questions JSON; not tokenizer output or full engine-rendered prompt"}


class SemanticServiceError(RuntimeError):
    """Explicit transport/protocol failure, with no fabricated model output."""
    def __init__(self, status, detail, http_status=None, input_metadata=None):
        super().__init__(detail)
        self.status, self.detail, self.http_status = status, detail, http_status
        self.input_metadata = deepcopy(input_metadata)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StartLuxHTTPClient:
    """Typed decision API client, not an OpenAI chat-completions adapter.

    The endpoint must be an explicit /v1/decisions or /v1/systemone URL. Proxy
    inheritance and redirects are disabled so local images stay at that target.
    No request content, response thought or API key is written to disk.
    """
    def __init__(self, endpoint="http://127.0.0.1:8090/v1/decisions", timeout_s=30.0,
                 expected_model=None, api_key=None, input_profile="full_provenance_v1"):
        if input_profile not in INPUT_PROFILES:
            raise ValueError("Unknown semantic input_profile")
        if input_profile=='focused_dual_history_v2':
            raise ValueError('Focused profile requires the reviewed-catalog client')
        self.profile = input_profile
        self.questions = PHASE_QUESTION if input_profile in PHASE_PROFILES else QUESTIONS
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password \
                or parsed.query or parsed.fragment or parsed.path not in ("/v1/decisions", "/v1/systemone"):
            raise ValueError("An explicit HTTP(S) StartLux decision endpoint without URL credentials is required")
        if not _finite(timeout_s) or not 0 < timeout_s <= 300:
            raise ValueError("timeout_s must be in (0, 300]")
        self.endpoint, self.timeout_s = endpoint, float(timeout_s)
        self.expected_model = _text(expected_model, "expected_model") if expected_model is not None else None
        if api_key is not None and (not isinstance(api_key, str) or not api_key or "\n" in api_key or "\r" in api_key):
            raise ValueError("Invalid API key")
        self._api_key = api_key
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @staticmethod
    def payload(snapshot):
        """Keep task/source identity in the model evidence as well as local receipts."""
        return {"state": {"observation_identity": _identity(snapshot), "evidence": snapshot["state"]},
                "images": snapshot["images"], "questions": deepcopy(QUESTIONS),
                "think_mode": "never", "allow_none": True, "none_check": True,
                "return_details": True, "return_thought": False}

    def decide(self, snapshot):
        metadata = {"input_profile": self.profile, "payload_text_json_chars": None,
                    "request_image_count": len(snapshot.get("images", [])) if isinstance(snapshot, dict) and
                    isinstance(snapshot.get("images", []), list) else None}
        try:
            snapshot = validate_snapshot(snapshot)
            derivation = None
            if self.profile in PHASE_PROFILES:
                payload, derivation = phase_payload(snapshot, self.profile)
            else:
                payload = self.request_payload(snapshot)
            metadata = _request_input(payload, self.profile)
            if derivation is not None:
                metadata['image_derivation'] = derivation
        except (ValueError, TypeError, KeyError, ImportError, OSError) as exc:
            raise SemanticServiceError("invalid_input", "Input profile rejected the supplied observation (" +
                                       type(exc).__name__ + "): " + str(exc)[:300], input_metadata=metadata) from None
        try:
            result = self._decide_payload(payload)
        except SemanticServiceError as exc:
            exc.input_metadata = metadata
            raise
        result.update(input_profile=self.profile, request_input=metadata)
        if self.profile in PHASE_PROFILES:
            result['not_requested'] = ['holding', 'placement']
            if self.profile != 'phase_single_replay_v1':
                from .semantic_motion_gate import phase_assessment
                result['phase_assessment'] = phase_assessment(result['answers']['phase'],
                    snapshot['state']['temporal'], snapshot['state']['right'])
        return result

    def request_payload(self, snapshot):
        """Select a profile explicitly; payload() itself remains the historical full form."""
        payload = self.payload(snapshot)
        if self.profile in PHASE_PROFILES:
            return phase_payload(snapshot, self.profile)[0]
        if self.profile == "compact_replay_v1":
            payload["state"] = _compact_replay_state(snapshot)
        return payload

    def _decide_payload(self, payload):
        payload = _json_bytes(payload)
        if len(payload) > MAX_REQUEST_BYTES:
            raise SemanticServiceError("invalid_request", "Request exceeds 32 MiB")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = "Bearer " + self._api_key
        request = Request(self.endpoint, data=payload, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise SemanticServiceError("invalid_response", "Decision response exceeds 1 MiB")
        except HTTPError as exc:
            code = int(exc.code)
            exc.close()
            raise SemanticServiceError("unavailable" if code in (429, 502, 503, 504) else "http_error",
                                       "StartLux HTTP status " + str(code), code) from None
        except (TimeoutError, socket.timeout):
            raise SemanticServiceError("timeout", "StartLux HTTP request timed out") from None
        except URLError as exc:
            timeout = isinstance(exc.reason, (TimeoutError, socket.timeout))
            raise SemanticServiceError("timeout" if timeout else "unavailable",
                                       "StartLux HTTP request timed out" if timeout else
                                       "StartLux endpoint could not be reached") from None
        except OSError:
            raise SemanticServiceError("unavailable", "StartLux connection failed") from None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise SemanticServiceError("invalid_response", "StartLux returned invalid UTF-8 JSON") from None
        return self.validate_response(body)

    def validate_response(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"]:
            raise SemanticServiceError("invalid_response", "Response has no model identity")
        if self.expected_model is not None and body["model"] != self.expected_model:
            raise SemanticServiceError("model_mismatch", "Response model does not match configured model")
        answers = body.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(self.questions):
            raise SemanticServiceError("invalid_response", "Response questions do not match the fixed observer contract")
        accepted = {}
        for key, question in self.questions.items():
            answer = answers[key]
            options = set(question["criteria"])
            if not isinstance(answer, dict) or answer.get("type") != "choice":
                raise SemanticServiceError("invalid_response", "Invalid choice answer: " + key)
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict) or set(probabilities) != options or any(
                    not _finite(v) or not 0 <= v <= 1 for v in probabilities.values()) or \
                    not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-4):
                raise SemanticServiceError("invalid_response", "Invalid probability distribution: " + key)
            choice, none = answer.get("choice"), answer.get("none_probability")
            confidence = answer.get("confidence")
            if "choice" not in answer or (choice is not None and choice not in options) or not _finite(none) or not 0 <= none <= 1 \
                    or not _finite(confidence) or not 0 <= confidence <= 1:
                raise SemanticServiceError("invalid_response", "Invalid choice/confidence/none score: " + key)
            if choice is not None and probabilities[choice] < max(probabilities.values()) - 1e-6:
                raise SemanticServiceError("invalid_response", "Choice contradicts the probability distribution: " + key)
            accepted[key] = {"type": "choice", "choice": choice, "confidence": confidence,
                             "probabilities": deepcopy(probabilities), "none_probability": none,
                             "interpretation": "model_candidate", "confirmed": False}
        timing = body.get("timing_ms")
        safe_timing = {key: value for key, value in timing.items() if isinstance(key, str) and
                       _finite(value) and value >= 0} if isinstance(timing, dict) else {}
        warnings = []
        if accepted.get("holding", {}).get("choice") == "held" and accepted.get("placement", {}).get("choice") == "in_bucket":
            warnings.append("模型同时给出持物与已释放入桶候选，需要复核")
        return {"status": "model_candidate", "model": body["model"][:256], "answers": accepted,
                "consistency_warnings": warnings,
                "timing_ms": safe_timing, "physical_task_result": "unverified",
                "motion_authority": False, "commands_sent": 0}


class SemanticObserver:
    """One HTTP request in flight and one replaceable latest pending snapshot.

    set_context invalidates old in-flight work without launching a second worker.
    max_age_s bounds local submit-to-result time, including queue residence. It
    does not establish remote camera age or prove replay freshness at the robot.
    """
    def __init__(self, client=None, max_age_s=30.0, event_limit=32):
        if not _finite(max_age_s) or not 0 < max_age_s <= 600:
            raise ValueError("max_age_s must be in (0, 600]")
        if isinstance(event_limit, bool) or not isinstance(event_limit, int) or not 1 <= event_limit <= 128:
            raise ValueError("event_limit must be in [1, 128]")
        self.client, self.max_age_s = client, float(max_age_s)
        self._condition = threading.Condition()
        self._context, self._generation, self._latest_sequence = None, 0, None
        self._pending, self._inflight, self._result, self._latest = None, None, None, None
        self._events = deque(maxlen=event_limit)
        self._closed = False
        self._submitted, self._replaced, self._discarded = 0, 0, 0
        self._worker = threading.Thread(target=self._run, name="semantic-side-channel", daemon=True)
        self._worker.start()

    def _event(self, kind, request_id=None, detail=None):
        self._events.append({"time_wall_s": time.time(), "kind": kind, "request_id": request_id,
                             "detail": None if detail is None else str(detail)[:300]})

    def set_context(self, source, task_id, session_id):
        if source not in ("live", "replay"):
            raise ValueError("source must be live or replay")
        context = (source, _text(task_id, "task_id"), _text(session_id, "session_id"))
        with self._condition:
            if self._closed:
                raise RuntimeError("Semantic observer is closed")
            if context != self._context:
                self._generation += 1
                self._context = context
                self._pending, self._result, self._latest = None, None, None
                self._latest_sequence = None
                self._event("context_changed")
                self._condition.notify_all()
            return self._generation

    def submit(self, snapshot):
        frozen = validate_snapshot(snapshot)
        context = tuple(frozen[key] for key in ("source", "task_id", "session_id"))
        with self._condition:
            if self._closed:
                raise RuntimeError("Semantic observer is closed")
            if context != self._context:
                raise ValueError("Snapshot context does not match the explicitly selected observer context")
            request_id = frozen["request_id"]
            if self._latest and request_id == self._latest["request_id"]:
                raise ValueError("request_id must change for each submitted snapshot")
            self._latest = _identity(frozen)
            self._submitted += 1
            self._latest_sequence = self._submitted
            submitted = time.monotonic()
            if self.client is None:
                self._result = {**_identity(frozen), "schema": SCHEMA, "status": "unavailable",
                                "error": {"status": "unavailable", "detail": "StartLux endpoint is not configured"},
                                "answers": {}, "model": None, "physical_task_result": "unverified",
                                "motion_authority": False, "commands_sent": 0,
                                "input_profile": "full_provenance_v1",
                                "request_input": {"input_profile": "full_provenance_v1", "payload_text_json_chars": None,
                                                  "request_image_count": len(frozen["images"])},
                                "sequence": self._submitted,
                                "_submitted_mono": submitted, "_completed_mono": submitted}
                self._event("unavailable", request_id)
                return {"accepted": False, "status": "unavailable", **_identity(frozen)}
            if self._pending is not None:
                self._replaced += 1
            self._pending = (frozen, self._generation, submitted, self._submitted)
            self._event("submitted", request_id)
            self._condition.notify_all()
            return {"accepted": True, "status": "queued", **_identity(frozen)}

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                item, self._pending = self._pending, None
                frozen, generation, submitted, sequence = item
                self._inflight = _identity(frozen)
            try:
                if time.monotonic() - submitted > self.max_age_s:
                    raise SemanticServiceError("expired", "Snapshot expired while waiting for the prior inference")
                result = self.client.decide(frozen)
            except SemanticServiceError as exc:
                result = {"status": exc.status, "error": {"status": exc.status, "detail": exc.detail[:600],
                          "http_status": exc.http_status}, "answers": {}, "model": None,
                          "request_input": deepcopy(exc.input_metadata)}
            except Exception as exc:
                result = {"status": "error", "error": {"status": "error", "detail":
                          "Observer request failed (" + type(exc).__name__ + ")"}, "answers": {}, "model": None}
            completed = time.monotonic()
            profile = getattr(self.client, "profile", "full_provenance_v1")
            result.setdefault("input_profile", profile)
            if result.get("request_input") is None:
                result["request_input"] = {"input_profile": profile, "payload_text_json_chars": None,
                                           "request_image_count": len(frozen["images"])}
            result = {**result, **_identity(frozen), "schema": SCHEMA,
                      "physical_task_result": "unverified", "motion_authority": False, "commands_sent": 0,
                      "sequence": sequence,
                      "_submitted_mono": submitted, "_completed_mono": completed}
            if completed - submitted > self.max_age_s and result["status"] == "model_candidate":
                result.update(status="expired", answers={}, error={"status": "expired",
                              "detail": "Late inference result exceeded the local processing deadline"})
            with self._condition:
                self._inflight = None
                if not self._closed and generation == self._generation:
                    self._result = result
                    self._event(result["status"], frozen["request_id"])
                else:
                    self._discarded += 1
                    self._event("discarded_old_context", frozen["request_id"])
                self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            result = deepcopy(self._result)
            if result is not None:
                now = time.monotonic()
                result["result_age_s"] = max(0.0, now - result.pop("_completed_mono"))
                result["request_age_s"] = max(0.0, now - result.pop("_submitted_mono"))
                result["matches_latest_request"] = result["sequence"] == self._latest_sequence
                result["within_processing_budget"] = result["request_age_s"] <= self.max_age_s
                result["source_age_s"] = None
                result["source_freshness"] = "historical_replay" if result["source"] == "replay" else "not_verified"
                result["fresh"] = False  # Never certify source freshness from the local HTTP completion clock.
            return {"schema": SCHEMA, "enabled": self.client is not None, "closed": self._closed,
                    "status": "closed" if self._closed else result["status"] if result is not None else
                              "processing" if self._inflight else "idle" if self.client is not None else "unavailable",
                    "context": None if self._context is None else dict(zip(
                        ("source", "task_id", "session_id"), self._context)),
                    "generation": self._generation, "result": result, "latest_source": deepcopy(self._latest),
                    "inflight": deepcopy(self._inflight),
                    "pending": None if self._pending is None else _identity(self._pending[0]),
                    "counts": {"submitted": self._submitted, "replaced_pending": self._replaced,
                               "discarded_old_context": self._discarded}, "events": deepcopy(list(self._events)),
                    "physical_task_result": "unverified", "motion_authority": False, "commands_sent": 0}

    def close(self):
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending = None
            self._condition.notify_all()
        self._worker.join(timeout=.1)  # In-flight HTTP stays bounded by the client's own timeout.
