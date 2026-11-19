"""Explicit replay phase profiles, using causal evidence and no stage rules.

Image mosaics are disclosed derived views; originals and their identities stay
in the replay receipt. No prior predictions, reference labels or future frames
are accepted. The phase question has no independent holding/success output.
"""
import base64
from copy import deepcopy
import hashlib
import io
import math


PHASE_PROFILES = ("phase_single_replay_v1", "temporal_phase_current_v1",
                  "temporal_phase_mosaic_v1", "temporal_phase_four_v1", "temporal_phase_mosaic_fast_v1",
                  "temporal_phase_mosaic_trim_v1")
TEMPORAL_PROFILES = PHASE_PROFILES[1:]
PHASE_QUESTION = {"phase": {
    "type": "choice",
    "instructions": "Classify the CURRENT observable phase of the RIGHT arm moving a red block into a green bucket. "
        "If past images exist, compare PAST to CURRENT; never classify the past frame. "
        "Actual joint/aperture changes help establish movement or opening, but closure alone does not prove holding. "
        "No motion before the task or after return is unknown. Use unknown for ambiguous evidence.",
    "criteria": {
        "approaching": "Empty right gripper approaching/alignment toward the red block before closing",
        "grasping": "Right gripper contacting or closing around red block, before lifting it away",
        "transporting": "Red block visibly held and being lifted/carried toward the bucket",
        "placing": "Held red block lowered at bucket or gripper opening/releasing it there",
        "returning": "Empty gripper actually moving away from bucket/returning after release",
        "unknown": "Stationary before approach or after return; insufficient/ambiguous/occluded evidence",
    }}}


def _round(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("Non-finite temporal feature")
        return round(value, 3)
    if isinstance(value, list):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    raise ValueError("Invalid temporal feature type")


def _verify_history(snapshot, features, evidence):
    from .temporal_observation import causal_features
    source = snapshot["stamps"]["camera_source_index_v1"]
    t, domain = source["sample_monotonic_ns"], source["clock_domain"]
    if evidence.get("clock_domain") != domain or evidence.get("current_sample_monotonic_ns") != t:
        raise ValueError("Temporal/current clocks differ")
    window = features.get("window_requested_s")
    if isinstance(window, bool) or not isinstance(window, (int, float)) or not math.isfinite(window) or not 0 < window <= 1:
        raise ValueError("Require a bounded causal window")
    rows = evidence.get("state_rows", [])
    stamps = [r.get("sample_monotonic_ns") for r in rows]
    if not rows or any(type(s) is not int or not max(0, t-round(window*1e9)) <= s <= t for s in stamps) or \
            any(b <= a for a, b in zip(stamps, stamps[1:])) or stamps[-1] != t:
        raise ValueError("History must be ordered, unique, in-window and end at current sample")
    records = []
    for row in rows:
        right = deepcopy(row["right"])
        for timing in right.get("source_timing", {}).values():
            if timing.pop("domain_matches_current", None) is not True:
                raise ValueError("History feedback source domain differs")
            timing["clock_domain"] = domain
        records.append({**row, "episode_id": snapshot["session_id"], "clock_domain": domain, "right": right})
    expected = causal_features(records, episode_id=snapshot["session_id"], sample_monotonic_ns=t,
                               clock_domain=domain, window_s=window)["features"]
    if type(features.get("sample_count")) is not int or expected != features:
        raise ValueError("Temporal features differ from actual source records")
    if any(rows[-1]["right"].get(k) != snapshot["state"]["right"].get(k) for k in ("q_deg", "grip_m")):
        raise ValueError("Current and temporal actual endpoints differ")
    has_past = evidence.get("past_images_available")
    if type(has_past) is not bool:
        raise ValueError("Past availability must be explicit")
    if not has_past:
        if snapshot["image_roles"] != ["front", "right"] or len(snapshot["images"]) != 2:
            raise ValueError("Unavailable history must contain only current images")
        return
    past = [r for r in rows if r["state_row"] == evidence.get("past_state_row")]
    if len(past) != 1:
        raise ValueError("Past image sample is missing from causal history")
    past_t = past[0]["sample_monotonic_ns"]
    gap = (t-past_t)/1e9
    requested = evidence.get("image_history_requested_s")
    if not isinstance(requested, (int, float)) or isinstance(requested, bool) or not .1 <= requested <= 1 or \
            not requested <= gap <= window or evidence.get("actual_image_pair_sample_gap_s") != gap:
        raise ValueError("Past image gap differs from source history")
    pair = evidence.get("image_pairs", [])
    if len(pair) != 2 or [p.get("role") for p in pair] != ["front", "right"]:
        raise ValueError("Past camera role order differs")
    for item in pair:
        stamp, available = item.get("source_monotonic_ns"), item.get("available_monotonic_ns")
        current_id = source["cameras"][item["role"]]["source_timing"]["sample_id"]
        if not isinstance(item.get("sample_id"), str) or not item["sample_id"] or item["sample_id"] == current_id or \
                type(stamp) is not int or type(available) is not int or not 0 <= stamp <= available <= past_t:
            raise ValueError("Past image identity/time differs from past source sample")


def phase_payload(snapshot, profile):
    # Import lazily to keep the existing observer's module dependency acyclic.
    from .semantic_observer import _compact_replay_state, _no_old_labels
    if profile not in PHASE_PROFILES:
        raise ValueError("Unknown phase profile")
    _no_old_labels(snapshot["state"])
    current = deepcopy(snapshot)
    current["images"] = snapshot["images"][-2:]
    current["image_roles"] = snapshot["image_roles"][-2:]
    state = _compact_replay_state(current)
    # Elapsed episode time is unnecessary for phase inference and can encourage
    # learning the sequence timing of two recordings. Keep it in local stamps.
    state.pop("relative_time_s", None)
    images = current["images"]
    derivation = None
    if profile in TEMPORAL_PROFILES:
        features = snapshot["state"].get("temporal")
        evidence = snapshot["stamps"].get("temporal_evidence")
        if not isinstance(features, dict) or not isinstance(evidence, dict):
            raise ValueError("Causal temporal evidence is required")
        _verify_history(snapshot, features, evidence)
        source = snapshot["stamps"]["camera_source_index_v1"]
        t = source["sample_monotonic_ns"]
        if evidence.get("clock_domain") != source["clock_domain"] or evidence.get("current_sample_monotonic_ns") != t:
            raise ValueError("Temporal/current clocks differ")
        rows = evidence.get("state_rows", [])
        if not rows or any(type(r.get("sample_monotonic_ns")) is not int or
                           r["sample_monotonic_ns"] > t for r in rows):
            raise ValueError("Temporal history must be non-empty and causal")
        state["past_1s_actual"] = _round({k: features.get(k) for k in (
            "sample_count", "actual_span_s", "joint_delta_deg", "joint_path_deg",
            "joint_mean_velocity_deg_s", "all_joint_rates_available", "gripper_rate_available")})
        grip = features.get("gripper")
        state["past_1s_actual"]["gripper"] = _round({
            "delta_mm": grip["delta_m"] * 1000,
            "first_mm": grip["first_m"] * 1000,
            "latest_mm": grip["latest_m"] * 1000,
            "trend": grip["trend"],
        }) if grip else {"available": False}
        has_past = evidence.get("past_images_available") is True
        if has_past:
            if snapshot["image_roles"] != ["past_front", "past_right", "front", "right"] or len(snapshot["images"]) != 4:
                raise ValueError("Require explicit past/current front/right order")
            pair = evidence.get("image_pairs", [])
            if len(pair) != 2 or any(type(p.get("source_monotonic_ns")) is not int or
                                    not 0 <= p["source_monotonic_ns"] <= p.get("available_monotonic_ns", -1) <= t for p in pair):
                raise ValueError("Past image source time is not causal")
            for p, uri in zip(pair, snapshot["images"][:2]):
                if hashlib.sha256(_raw(uri)).hexdigest() != p.get("jpeg_sha256"):
                    raise ValueError("Past image bytes differ from temporal identity")
        state["past_images_available"] = has_past
        state["past_image_gap_s"] = _round(evidence.get("actual_image_pair_sample_gap_s"))
        if profile == "temporal_phase_four_v1" and has_past:
            images = snapshot["images"]
            state["image_order"] = snapshot["image_roles"]
        elif profile in ("temporal_phase_mosaic_v1", "temporal_phase_mosaic_fast_v1", "temporal_phase_mosaic_trim_v1") and has_past:
            images, derivation = mosaic(snapshot["images"], size=(768,576) if profile == "temporal_phase_mosaic_fast_v1" else (896,672))
            state["image_order"] = ["2x2 mosaic: top PAST front/right; bottom CURRENT front/right"]
            state["derived_image"] = {"size_px": derivation["size_px"], "each_view_px": derivation["tile_px"],
                                      "method": "Resize whole views, no crop; labels only camera and time"}
        state["limitations"] = "Current and past only. Source software times are not hardware exposure times. Gripper closure is not contact; joint motion is not task success."
        if profile == 'temporal_phase_mosaic_trim_v1':
            # These values remain exact in stamps/receipts; avoid repeating the
            # original-image dimensions for a disclosed derived mosaic.
            state.pop('image_quality_at_recording', None)
            state.pop('hardware_exposure_timestamp_available', None)
            state['limitations'] = 'Causal software-timed observations only; physical result unverified.'
    return {"state": state, "images": images, "questions": deepcopy(PHASE_QUESTION),
            "think_mode": "never", "allow_none": True, "none_check": True,
            "return_details": True, "return_thought": False}, derivation


def _raw(uri):
    return base64.b64decode(uri.split(",", 1)[1] if uri.startswith("data:") else uri, validate=True)


def mosaic(images, size=(896,672)):
    """Whole-view 2x2, with no object detector or task-dependent cropping."""
    from PIL import Image, ImageDraw
    if len(images) != 4:
        raise ValueError("A temporal mosaic requires four distinct original views")
    if size not in ((896,672), (768,576)):
        raise ValueError("Unsupported, undisclosed mosaic size")
    width, height = size
    tile = (width // 2, height // 2)
    canvas = Image.new("RGB", (width, height))
    provenance = []
    roles = ("PAST front", "PAST right", "CURRENT front", "CURRENT right")
    for i, (uri, role) in enumerate(zip(images, roles)):
        raw = _raw(uri)
        with Image.open(io.BytesIO(raw)) as original:
            original.load()
            if original.size[0] * tile[1] != original.size[1] * tile[0]:
                raise ValueError("Mosaic whole-view aspect ratio differs")
            view = original.convert("RGB").resize(tile, Image.Resampling.LANCZOS)
            provenance.append({"role": role, "original_sha256": hashlib.sha256(raw).hexdigest(),
                               "original_size_px": list(original.size), "tile_xy": [(i % 2) * tile[0], (i // 2) * tile[1]]})
        canvas.paste(view, ((i % 2) * tile[0], (i // 2) * tile[1]))
    draw = ImageDraw.Draw(canvas)
    for i, role in enumerate(roles):
        x, y = (i % 2) * tile[0], (i // 2) * tile[1]
        draw.rectangle((x, y, x + 112, y + 13), fill="black")
        draw.text((x + 2, y + 1), role, fill="white")
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=95, subsampling=0)
    raw = buffer.getvalue()
    return ["data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")], {
        "kind": "whole-view-temporal-mosaic", "size_px": [width, height], "tile_px": list(tile),
        "jpeg_quality": 95, "subsampling": 0, "output_sha256": hashlib.sha256(raw).hexdigest(),
        "inputs": provenance, "raw_sources_modified": False}
