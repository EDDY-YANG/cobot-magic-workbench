"""Read explicitly reviewed historical observations without devices or raw writes.

The source-review receipt selects files and real frame offsets. It is a smoke
input catalog, never ground truth for phase recognition. USB identity and full
source hashes are checked at construction; per-call stat and JPEG hashes reject
changed/disappearing files. Recorded source clocks remain opaque source clocks.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import ctypes
import hashlib
import json
import math
from pathlib import Path
import struct

from .temporal_observation import actual_record, causal_features


SOURCE_SERIAL = "67452301"
REVIEW_SCHEMA = "tiger-startlux-two-episode-readonly-review-v1"
ROLES = ("front", "right")
SOURCE_FILES = ("operation.json", "state-0000.bin", "front-0000.bin", "right-0000.bin")
MAX_PAYLOAD = 8 * 1024 * 1024


class ReplaySourceError(RuntimeError):
    """A missing, changed, unreviewed, or malformed source is not replayable."""


def _require(value, message):
    if not value:
        raise ReplaySourceError(message)


def _sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _stat(path):
    try:
        value = path.stat()
    except OSError as exc:
        raise ReplaySourceError("Replay source is unavailable: " + str(path)) from exc
    _require(path.is_file(), "Replay source must be a regular file")
    return value.st_size, value.st_mtime_ns


def _volume_identity(root):
    """One native Windows volume query per catalog; never chooses another disk."""
    serial = ctypes.c_ulong()
    filesystem = ctypes.create_unicode_buffer(32)
    try:
        success = ctypes.windll.kernel32.GetVolumeInformationW(
            str(root), None, 0, ctypes.byref(serial), None, None,
            filesystem, len(filesystem))
    except (AttributeError, OSError) as exc:
        raise ReplaySourceError("Windows source-volume identity is unavailable") from exc
    _require(success, "Cannot read the selected USB volume identity")
    return {"serial": f"{serial.value:08X}", "filesystem": filesystem.value}


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReplaySourceError("Invalid JSON source: " + str(path)) from exc


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _chunks(path):
    with path.open("rb") as stream:
        previous = None
        while True:
            header = stream.read(12)
            if not header:
                return
            _require(len(header) == 12, "Truncated state header")
            stamp, length = struct.unpack("<QI", header)
            _require(stamp > 0 and 0 < length <= MAX_PAYLOAD, "Invalid state chunk bounds")
            _require(previous is None or stamp > previous, "State timestamps must increase")
            payload = stream.read(length)
            _require(len(payload) == length, "Truncated state payload")
            previous = stamp
            try:
                value = json.loads(payload)
            except (UnicodeError, ValueError) as exc:
                raise ReplaySourceError("Invalid state JSON") from exc
            yield stamp, value


class ReplayCatalog:
    """Bounded catalog of explicitly selected, source-hash-verified observations.

    ``list()`` returns presentation metadata. ``observation(id, index)`` returns
    a fresh deep copy matching SemanticObserver's snapshot protocol, with two
    original JPEG data URIs. ``image_bytes`` returns one original JPEG payload.
    No caller can submit an arbitrary file path through these public methods.
    ``expected_review_sha256`` can pin the PC configuration to a reviewed receipt.
    """

    def __init__(self, source_review_path, expected_review_sha256=None):
        try:
            self._review_path = Path(source_review_path).resolve(strict=True)
        except OSError as exc:
            raise ReplaySourceError("Configured source-review receipt is unavailable") from exc
        self._guards = {}
        self._episodes = {}
        review_sha = _sha(self._review_path)
        if expected_review_sha256 is not None:
            _require(review_sha == expected_review_sha256, "Source-review SHA changed")
        review = _read_json(self._review_path)
        _require(review.get("schema") == REVIEW_SCHEMA, "Unsupported source-review schema")
        source = review.get("source_volume", {})
        _require(source.get("serial") == SOURCE_SERIAL and
                 str(source.get("filesystem", "")).lower() == "exfat", "Receipt USB identity mismatch")
        self._source_root = Path(source.get("root", ""))
        _require(self._source_root.is_absolute() and self._source_root == Path(self._source_root.anchor),
                 "An absolute selected USB root is required")
        identity = _volume_identity(self._source_root)
        _require(identity.get("serial") == SOURCE_SERIAL and
                 str(identity.get("filesystem", "")).lower() == "exfat", "Selected USB identity changed")
        self._review_sha256 = review_sha
        self._guards[self._review_path] = _stat(self._review_path)
        freeze_path = self._source_path(review.get("freeze_path", ""))
        manifest_path = self._source_path(review.get("split_manifest_path", ""))
        self._verify_file(freeze_path, review.get("freeze_sha256"))
        self._verify_file(manifest_path, review.get("split_manifest_sha256"))
        frozen = {item["episode_id"]: item for item in _read_json(freeze_path)["rows"]}
        manifest = {item["episode_id"]: item for item in _read_json(manifest_path)["episodes"]}
        selected = review.get("episodes")
        _require(isinstance(selected, list) and 0 < len(selected) <= 2, "Select one or two reviewed episodes")
        for episode in selected:
            self._load_episode(episode, frozen, manifest)
        self._guard()

    def _source_path(self, value):
        _require(isinstance(value, str) and value, "An explicit source path is required")
        try:
            path = Path(value).resolve(strict=True)
        except OSError as exc:
            raise ReplaySourceError("Configured replay source is unavailable: " + value) from exc
        _require(path.is_relative_to(self._source_root), "Source escapes the selected USB volume")
        return path

    def _verify_file(self, path, expected_sha, expected_size=None, expected_mtime=None):
        before = _stat(path)
        _require(isinstance(expected_sha, str) and len(expected_sha) == 64, "Missing frozen source SHA")
        _require(expected_size is None or before[0] == expected_size, "Frozen source size changed")
        _require(expected_mtime is None or before[1] == expected_mtime, "Frozen source mtime changed")
        _require(_sha(path) == expected_sha, "Frozen source SHA changed: " + str(path))
        _require(_stat(path) == before, "Source changed during hash verification")
        self._guards[path] = before

    def _load_episode(self, reviewed, frozen, manifest):
        episode_id = reviewed.get("episode_id")
        _require(isinstance(episode_id, str) and 0 < len(episode_id) < 160 and
                 Path(episode_id).name == episode_id and "/" not in episode_id and "\\" not in episode_id,
                 "Invalid episode identity")
        _require(episode_id not in self._episodes and episode_id in frozen and episode_id in manifest,
                 "Duplicate or unfrozen episode")
        truth, split = frozen[episode_id], manifest[episode_id]
        _require(truth.get("verdict") == "success" and truth.get("is_test") is False and
                 split.get("split") in ("train", "validation") and reviewed.get("split") == split["split"] and
                 reviewed.get("ordinal") == truth.get("ordinal") == split.get("ordinal"),
                 "Episode is outside the reviewed non-test scope")
        root = self._source_path(reviewed.get("raw_path", ""))
        _require(root.is_dir() and root.name == episode_id, "Raw directory identity mismatch")
        files = reviewed.get("files", {})
        _require(set(files) == set(SOURCE_FILES), "Unexpected raw file scope")
        expected_sources = {item["name"]: item["sha256"] for item in
                            split["native_state_files"] + split["source_files"]}
        expected_sources["operation.json"] = split["operation_sha256"]
        _require(expected_sources["operation.json"] == truth["receipt_sha256"],
                 "Review no longer matches the operation")
        for name in SOURCE_FILES:
            path = self._source_path(str(root / name))
            record = files[name]
            _require(record.get("sha256") == expected_sources[name], "Raw SHA disagrees with split manifest")
            self._verify_file(path, record["sha256"], record["bytes"], record["mtime_ns"])
        operation = _read_json(root / "operation.json")
        _require(operation.get("format") == "tiger-native-sensors-v1" and operation.get("ended_at") and
                 operation.get("failure") is None and operation.get("queue_overflows") == 0,
                 "Only finalized native recordings without capture failures are accepted")
        wanted = reviewed.get("suggested_observations")
        _require(isinstance(wanted, list) and 0 < len(wanted) <= 600, "Missing bounded real observations")
        rows = {item["state_row"]: item for item in wanted}
        _require(len(rows) == len(wanted) and all(type(n) is int and n >= 0 for n in rows),
                 "Duplicate or invalid state row")
        observed, unique, first_stamp, history = {}, {role: set() for role in ROLES}, None, []
        for n, (stamp, state) in enumerate(_chunks(root / "state-0000.bin")):
            if first_stamp is None:
                first_stamp = stamp
            history.append(actual_record(state, episode_id, n))
            if n not in rows:
                continue
            preview = rows[n]
            _require(stamp == preview.get("sample_wall_ns") == state.get("sample_wall_ns"),
                     "Frozen state timestamp differs")
            arm = state.get("sensors", {}).get("arms", {}).get("right", {})
            q, grip = arm.get("q_deg"), arm.get("grip_m")
            _require(isinstance(q, list) and len(q) == 6 and all(_finite(v) for v in q) and _finite(grip) and
                     arm.get("feedback_fresh") is True and _finite(arm.get("age_joint_s")) and
                     0 <= arm["age_joint_s"] <= .1, "Missing finite fresh actual right-arm feedback")
            index = state.get("camera_source_index_v1", {})
            domain = index.get("clock_domain", {})
            _require(domain.get("identity_verified") is True, "Unverified source clock domain")
            cameras = index.get("cameras", {})
            sample_mono = index.get("sample_monotonic_ns")
            _require(type(sample_mono) is int and _finite(preview.get("offset_s")) and
                     math.isclose(preview["offset_s"], (stamp - first_stamp) / 1e9, abs_tol=1e-8),
                     "Invalid replay clock or time offset")
            for role in ROLES:
                supplied, source = preview["images"][role], cameras.get(role, {})
                timing = source.get("source_timing", {})
                source_mono, available = timing.get("source_monotonic_ns_estimate"), source.get("available_monotonic_ns")
                _require(supplied.get("wall_ns") == source.get("wall_ns") == state.get("camera_sample_ns", {}).get(role)
                         and supplied.get("sample_id") == timing.get("sample_id")
                         and supplied.get("frame_id") == source.get("frame_id")
                         and timing.get("clock_domain") == domain and isinstance(supplied.get("sample_id"), str)
                         and supplied["sample_id"] and supplied["sample_id"] not in unique[role]
                         and type(source_mono) is int and type(available) is int
                         and 0 <= source_mono <= available <= sample_mono
                         and sample_mono - source_mono <= 100_000_000 and supplied["wall_ns"] <= stamp,
                         "Image identity mismatch or duplicate source frame")
                unique[role].add(supplied["sample_id"])
            actual = {"q_deg": list(q), "grip_m": grip, "feedback_fresh": True}
            for key in ("age_joint_s", "age_gripper_s", "age_status_s"):
                if _finite(arm.get(key)):
                    actual[key] = arm[key]
            if isinstance(arm.get("source_timing"), dict):
                actual["source_timing"] = deepcopy(arm["source_timing"])
            observed[n] = {"source": "replay", "task_id": operation["task"],
                           "session_id": "replay:" + self._review_sha256[:24],
                           "request_id": episode_id + ":row:" + str(n),
                           "frame_id": episode_id + ":row:" + str(n),
                           "stamps": {"sample_wall_ns": stamp,
                                      "camera_source_index_v1": deepcopy(index),
                                      "offset_s": preview["offset_s"],
                                      "hardware_exposure_timestamp_available":
                                          operation.get("hardware_exposure_timestamp_available")},
                           "state": {"right": actual}, "images": [], "image_roles": list(ROLES)}
        _require(len(observed) == len(wanted), "Frozen state row is absent")
        self._episodes[episode_id] = {"root": root, "history": history, "source_files": deepcopy(files),
                                      "observations": [observed[s["state_row"]] for s in wanted],
                                      "samples": deepcopy(wanted), "metadata": {
                                          "episode_id": episode_id, "ordinal": reviewed["ordinal"],
                                          "source": "replay", "split": split["split"],
                                          "task": operation["task"], "duration_s": operation["elapsed_s"],
                                          "observation_count": len(wanted), "image_roles": list(ROLES),
                                          "historical_recording": True}}
        for n in range(len(wanted)):
            for role in ROLES:
                self._image_bytes(episode_id, n, role)

    def _guard(self):
        _require(self._source_root.exists(), "Selected USB mount is unavailable; no fallback is allowed")
        for path, expected in self._guards.items():
            _require(_stat(path) == expected, "Frozen replay source changed: " + str(path))

    def _entry(self, episode_id, index):
        _require(isinstance(episode_id, str) and episode_id in self._episodes, "Unknown replay episode")
        episode = self._episodes[episode_id]
        _require(type(index) is int and 0 <= index < len(episode["samples"]), "Invalid replay observation index")
        return episode, episode["samples"][index]

    def list(self):
        self._guard()
        return deepcopy([item["metadata"] for item in self._episodes.values()])

    def _image_bytes(self, episode_id, index, role):
        _require(role in ROLES, "Unknown replay camera role")
        episode, sample = self._entry(episode_id, index)
        frame = sample["images"][role]
        offset = frame.get("offset")
        _require(type(offset) is int and offset >= 0, "Invalid JPEG chunk offset")
        with (episode["root"] / (role + "-0000.bin")).open("rb") as stream:
            stream.seek(offset)
            header = stream.read(12)
            _require(len(header) == 12, "Truncated JPEG header")
            stamp, length = struct.unpack("<QI", header)
            _require(stamp == frame["wall_ns"] and length == frame["jpeg_bytes"] and
                     0 < length <= MAX_PAYLOAD, "JPEG header differs from reviewed source")
            payload = stream.read(length)
        _require(len(payload) == length and payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9") and
                 hashlib.sha256(payload).hexdigest() == frame["jpeg_sha256"], "JPEG payload differs from reviewed source")
        return payload

    def image_bytes(self, episode_id, index, role):
        self._guard()
        payload = self._image_bytes(episode_id, index, role)
        self._guard()
        return payload

    def observation(self, episode_id, index):
        self._guard()
        episode, _ = self._entry(episode_id, index)
        value = deepcopy(episode["observations"][index])
        value["images"] = ["data:image/jpeg;base64," + base64.b64encode(
            self._image_bytes(episode_id, index, role)).decode("ascii") for role in ROLES]
        self._guard()
        return value

    def _history_image_index(self, episode):
        """Index actual original JPEG chunks once, against the frozen file SHA."""
        if "history_image_index" in episode:
            return episode["history_image_index"]
        result = {}
        for role in ROLES:
            path = episode["root"] / (role + "-0000.bin")
            _require(_sha(path) == episode["source_files"][role + "-0000.bin"]["sha256"],
                     "Frozen history JPEG source SHA changed")
            frames = {}
            with path.open("rb") as stream:
                while True:
                    offset = stream.tell()
                    header = stream.read(12)
                    if not header:
                        break
                    _require(len(header) == 12, "Truncated history JPEG header")
                    stamp, length = struct.unpack("<QI", header)
                    _require(stamp > 0 and 0 < length <= MAX_PAYLOAD and stamp not in frames,
                             "Invalid or duplicate history JPEG chunk")
                    payload = stream.read(length)
                    _require(len(payload) == length and payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9"),
                             "Malformed original history JPEG")
                    frames[stamp] = {"wall_ns": stamp, "offset": offset, "jpeg_bytes": length,
                                     "jpeg_sha256": hashlib.sha256(payload).hexdigest()}
            result[role] = frames
        self._guard()
        episode["history_image_index"] = result
        return result

    def _history_image_bytes(self, episode, role, frame):
        with (episode["root"] / (role + "-0000.bin")).open("rb") as stream:
            stream.seek(frame["offset"])
            header = stream.read(12)
            _require(header == struct.pack("<QI", frame["wall_ns"], frame["jpeg_bytes"]),
                     "History JPEG header changed")
            payload = stream.read(frame["jpeg_bytes"])
        _require(hashlib.sha256(payload).hexdigest() == frame["jpeg_sha256"], "History JPEG payload changed")
        return payload

    def temporal_observation(self, episode_id, index, *, window_s=1.0, image_history_s=.75):
        """Current reviewed point plus strictly causal real history, never labels.

        Original ``observation`` remains unchanged. If a distinct source pair
        exists at/before t-image_history_s, order is past_front, past_right,
        front, right; otherwise current images alone and an explicit reason.
        All history comes from the same frozen non-test episode. The historical
        clocks remain opaque to PC clocks and do not imply live freshness.
        """
        _require(_finite(window_s) and 0 < window_s <= 1,
                 "Replay temporal window_s must be finite and in (0, 1]")
        _require(_finite(image_history_s) and .1 <= image_history_s <= 1,
                 "image_history_s must be finite and in [0.1, 1]")
        value = self.observation(episode_id, index)
        episode, _ = self._entry(episode_id, index)
        source_index = value["stamps"]["camera_source_index_v1"]
        mono, domain = source_index["sample_monotonic_ns"], source_index["clock_domain"]
        temporal = causal_features(episode["history"], episode_id=episode_id,
                                   sample_monotonic_ns=mono, clock_domain=domain, window_s=window_s)
        value["state"]["temporal"] = temporal["features"]
        rows = []
        for row in temporal["history"]:
            right = row["right"]
            rows.append({"state_row": row["state_row"], "sample_wall_ns": row["sample_wall_ns"],
                         "sample_monotonic_ns": row["sample_monotonic_ns"],
                         "relative_to_current_s": (row["sample_monotonic_ns"] - mono) / 1e9,
                         "right": deepcopy(right)})
            # The domain is frozen once per evidence window, not duplicated 4x
            # per row; these are exact source clocks, q and aperture values.
            for timing in rows[-1]["right"]["source_timing"].values():
                timing["domain_matches_current"] = timing.pop("clock_domain", None) == domain
        evidence = {"schema": temporal["features"]["schema"], "clock_domain": deepcopy(domain),
                    "current_sample_monotonic_ns": mono, "state_rows": rows,
                    "rejected_rows": temporal["rejected_rows"], "image_history_requested_s": image_history_s,
                    "past_images_available": False, "image_pairs": [],
                    "limitations": "Only current and past observations; no interpolation or future frames. Source times are host software/kernel estimates."}
        # Select a pair using sample time, then independently validate both
        # original camera identities/times. Never choose a closer future row.
        target = mono - round(image_history_s * 1e9)
        candidates = [row for row in temporal["history"] if row["sample_monotonic_ns"] <= target]
        image_index = None
        for row in reversed(candidates):
            pair = []
            for role in ROLES:
                camera = row["cameras"].get(role, {})
                timing = camera.get("source_timing", {})
                stamp, available = timing.get("source_monotonic_ns_estimate"), camera.get("available_monotonic_ns")
                current_id = source_index["cameras"][role]["source_timing"]["sample_id"]
                if (timing.get("clock_domain") != domain or not isinstance(timing.get("sample_id"), str)
                        or not timing["sample_id"] or timing["sample_id"] == current_id
                        or type(stamp) is not int or type(available) is not int
                        or not 0 <= stamp <= available <= row["sample_monotonic_ns"]
                        or row["sample_monotonic_ns"] - stamp > 100_000_000
                        or type(camera.get("wall_ns")) is not int or camera["wall_ns"] > row["sample_wall_ns"]):
                    break
                if image_index is None:
                    image_index = self._history_image_index(episode)
                frame = image_index[role].get(camera["wall_ns"])
                if frame is None:
                    break
                pair.append({**deepcopy(frame), "role": role, "sample_id": timing["sample_id"],
                             "frame_id": camera.get("frame_id"), "source_monotonic_ns": stamp,
                             "available_monotonic_ns": available,
                             "age_ms_at_recorded_sample": (row["sample_monotonic_ns"] - stamp) / 1e6})
            if len(pair) != len(ROLES):
                continue
            past_images = ["data:image/jpeg;base64," + base64.b64encode(
                self._history_image_bytes(episode, frame["role"], frame)).decode("ascii") for frame in pair]
            value["images"] = past_images + value["images"]
            value["image_roles"] = ["past_front", "past_right", "front", "right"]
            evidence.update({"past_images_available": True,
                             "past_state_row": row["state_row"],
                             "actual_image_pair_sample_gap_s": (mono - row["sample_monotonic_ns"]) / 1e9,
                             "image_pairs": pair})
            break
        if not evidence["past_images_available"]:
            evidence["past_images_unavailable_reason"] = "No distinct valid same-domain past front/right pair within this causal window"
        value["stamps"]["temporal_evidence"] = evidence
        self._guard()
        return value
