"""Cheap, causal image-coordinate colour candidates, never object/task truth.

The thresholds describe broad colours in HSV, not a trained object detector.
In particular a red centroid inside a green bounding box does not prove a
placement: projection, occlusion, illumination and unrelated colours matter.
This pure module accepts bytes and source identities; it opens no files or
devices, uses no model, and does not classify a robot phase.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from io import BytesIO
import math


SCHEMA = "tiger-image-colour-candidates-v1"
# Pillow's HSV channels are integers in [0, 255], including hue.
THRESHOLDS = {
    "red": {"hue_ranges_255": ((0, 15), (240, 255)), "saturation_min_255": 70, "value_min_255": 30},
    "green": {"hue_ranges_255": ((45, 135),), "saturation_min_255": 35, "value_min_255": 20},
}
LIMITATIONS = (
    "Broad colour candidates are not verified object identities. Centroid/bbox distances are image pixels, not millimetres. "
    "A wrist camera moves with the robot; pixel displacement is not world/object velocity. "
    "A red centroid within a green bounding box is a 2D projection relation, not proof of containment, contact or task success. "
    "Dark/occluded/low-saturation regions can disappear; similar-coloured background can produce false candidates. "
    "No gripper identity is inferred from dark pixels."
)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _domain_valid(domain):
    return (isinstance(domain, dict) and domain.get("identity_verified") is True
            and isinstance(domain.get("session_id"), str) and bool(domain["session_id"]))


def _components(mask, width, height, minimum, limit, original_size):
    """8-connected components on a bounded analysis image; no dependencies."""
    remaining = bytearray(mask)
    components = []
    raw_count = 0
    scale_x, scale_y = original_size[0] / width, original_size[1] / height
    for seed, present in enumerate(remaining):
        if not present:
            continue
        remaining[seed] = 0
        stack = [seed]
        count = sum_x = sum_y = 0
        xmin, ymin, xmax, ymax = width, height, -1, -1
        while stack:
            item = stack.pop()
            y, x = divmod(item, width)
            count += 1
            sum_x += x
            sum_y += y
            xmin, ymin, xmax, ymax = min(xmin, x), min(ymin, y), max(xmax, x), max(ymax, y)
            for ny in range(max(0, y - 1), min(height, y + 2)):
                base = ny * width
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = base + nx
                    if remaining[neighbor]:
                        remaining[neighbor] = 0
                        stack.append(neighbor)
        raw_count += 1
        if count < minimum:
            continue
        touches_border = xmin == 0 or ymin == 0 or xmax == width - 1 or ymax == height - 1
        components.append({
            "bbox_image_px_xyxy": [round(xmin * scale_x, 3), round(ymin * scale_y, 3),
                                    round((xmax + 1) * scale_x, 3), round((ymax + 1) * scale_y, 3)],
            "centroid_image_px_xy": [round((sum_x / count + .5) * scale_x, 3),
                                     round((sum_y / count + .5) * scale_y, 3)],
            "area_fraction": round(count / (width * height), 7),
            "analysis_pixels": count,
            "bbox_mask_fill_fraction": round(count / ((xmax - xmin + 1) * (ymax - ymin + 1)), 5),
            "touches_image_border": touches_border,
            "object_identity_verified": False,
        })
    components.sort(key=lambda candidate: candidate["analysis_pixels"], reverse=True)
    count = len(components)
    return {"candidates": components[:limit], "retained_component_count": count,
            "small_components_discarded": raw_count - count, "truncated": count > limit,
            "multiple_candidates": count > 1,
            "quality": "missing" if count == 0 else "ambiguous_colour_identity"}


def _spatial_relation(colours, original_size):
    red, green = colours["red"]["candidates"], colours["green"]["candidates"]
    if not red or not green:
        return {"available": False, "reason": "At least one colour has no retained candidate"}
    x, y = red[0]["centroid_image_px_xy"]
    gx, gy = green[0]["centroid_image_px_xy"]
    x0, y0, x1, y1 = green[0]["bbox_image_px_xyxy"]
    return {"available": True, "candidate_rule": "largest retained component of each colour",
            "red_centroid_in_green_candidate_bbox_2d": x0 <= x < x1 and y0 <= y < y1,
            "centroid_distance_image_px": round(math.hypot(x - gx, y - gy), 3),
            "centroid_distance_image_diagonals": round(math.hypot(x - gx, y - gy) / math.hypot(*original_size), 5),
            "object_identity_verified": False, "physical_containment_verified": False}


def colour_evidence(image_bytes, *, camera_role, source_monotonic_ns, sample_id, clock_domain,
                    max_side=256, min_area_fraction=.0004, max_candidates=5):
    """Extract only colour candidates from one image with an explicit source.

    ``clock_domain`` is the caller's original verified source domain. The
    source timestamp is never compared with a replay-PC clock. Image resizing
    preserves the entire field of view. Returned coordinates refer to the
    original image, with analysis resolution retained for uncertainty review.
    """
    if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= 8 * 1024 * 1024:
        raise ValueError("An image payload of at most 8 MiB is required")
    if (not isinstance(camera_role, str) or not camera_role
            or type(source_monotonic_ns) is not int or source_monotonic_ns < 0
            or not isinstance(sample_id, str) or not sample_id or not _domain_valid(clock_domain)):
        raise ValueError("A camera, unique source identity and verified original clock domain are required")
    if type(max_side) is not int or not 64 <= max_side <= 512:
        raise ValueError("max_side must be an integer in [64, 512]")
    if not _finite(min_area_fraction) or not 0 < min_area_fraction <= .05:
        raise ValueError("min_area_fraction must be finite and in (0, .05]")
    if type(max_candidates) is not int or not 1 <= max_candidates <= 10:
        raise ValueError("max_candidates must be an integer in [1, 10]")
    from PIL import Image, ImageChops
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > 16 * 1024 * 1024:
                raise ValueError("Image dimensions are outside the bounded feature budget")
            rgb = source.convert("RGB")
    except (OSError, SyntaxError) as exc:
        raise ValueError("Cannot decode the supplied image payload") from exc
    scale = min(1.0, max_side / max(width, height))
    analysis_size = max(1, round(width * scale)), max(1, round(height * scale))
    rgb = rgb.resize(analysis_size, Image.Resampling.BILINEAR)
    hue, saturation, value = rgb.convert("HSV").split()
    pixels = analysis_size[0] * analysis_size[1]
    minimum = max(3, math.ceil(min_area_fraction * pixels))
    colours = {}
    for name, limits in THRESHOLDS.items():
        hue_mask = hue.point([255 if any(a <= item <= b for a, b in limits["hue_ranges_255"]) else 0 for item in range(256)])
        sat_mask = saturation.point([255 if item >= limits["saturation_min_255"] else 0 for item in range(256)])
        val_mask = value.point([255 if item >= limits["value_min_255"] else 0 for item in range(256)])
        mask = ImageChops.multiply(ImageChops.multiply(hue_mask, sat_mask), val_mask).tobytes()
        colours[name] = _components(mask, *analysis_size, minimum, max_candidates, (width, height))
        colours[name]["threshold_mask_area_fraction"] = round(sum(item != 0 for item in mask) / pixels, 7)
    return {"schema": SCHEMA, "camera_role": camera_role, "source_monotonic_ns": source_monotonic_ns,
            "sample_id": sample_id, "clock_domain": deepcopy(clock_domain),
            "image_sha256": hashlib.sha256(image_bytes).hexdigest(), "image_size_px": [width, height],
            "analysis_size_px": list(analysis_size), "min_analysis_component_pixels": minimum,
            "thresholds_hsv_255": deepcopy(THRESHOLDS), "colours": colours,
            "spatial_relation": _spatial_relation(colours, (width, height)), "limitations": LIMITATIONS}


def causal_colour_change(previous, current, *, max_gap_s=2.0):
    """Image displacement of largest same-colour candidates, never tracking.

    No correspondence or scene motion compensation is invented. Repeated
    source IDs, future frames, mixed cameras/domains, dimension changes and
    overlong gaps produce an explicit unavailable result.
    """
    if not _finite(max_gap_s) or not 0 < max_gap_s <= 10:
        raise ValueError("max_gap_s must be finite and in (0, 10]")
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("Two colour-evidence objects are required")
    reason = None
    if previous.get("schema") != SCHEMA or current.get("schema") != SCHEMA:
        reason = "Unsupported colour-evidence schema"
    elif any(not isinstance(item.get("sample_id"), str) or not item["sample_id"]
             or not isinstance(item.get("camera_role"), str) or not item["camera_role"]
             for item in (previous, current)):
        reason = "Both frames require a real source identity and camera role"
    elif not _domain_valid(current.get("clock_domain")) or previous.get("clock_domain") != current["clock_domain"]:
        reason = "Clock domains differ or are unverified"
    elif previous.get("camera_role") != current.get("camera_role"):
        reason = "Camera roles differ"
    elif previous.get("sample_id") == current.get("sample_id"):
        reason = "Repeated source identity cannot add temporal evidence"
    elif previous.get("image_size_px") != current.get("image_size_px"):
        reason = "Image dimensions changed"
    else:
        before, now = previous.get("source_monotonic_ns"), current.get("source_monotonic_ns")
        if type(before) is not int or type(now) is not int or not 0 <= before < now:
            reason = "Past source time must be strictly earlier than current source time"
        elif (now - before) / 1e9 > max_gap_s:
            reason = "Source gap exceeds the causal comparison budget"
    if reason:
        return {"available": False, "reason": reason}
    span = (current["source_monotonic_ns"] - previous["source_monotonic_ns"]) / 1e9
    changes = {}
    for name in THRESHOLDS:
        old = previous.get("colours", {}).get(name, {}).get("candidates", [])
        new = current.get("colours", {}).get(name, {}).get("candidates", [])
        if not old or not new:
            changes[name] = {"available": False, "reason": "Candidate missing in one or both images"}
            continue
        displacement = [b - a for a, b in zip(old[0]["centroid_image_px_xy"], new[0]["centroid_image_px_xy"])]
        changes[name] = {"available": True,
                         "largest_candidate_centroid_delta_image_px_xy": [round(item, 3) for item in displacement],
                         "largest_candidate_area_fraction_delta": round(new[0]["area_fraction"] - old[0]["area_fraction"], 7),
                         "correspondence_verified": False,
                         "multiple_candidates_in_either_image": len(old) > 1 or len(new) > 1}
    return {"available": True, "source_span_s": span, "colours": changes,
            "source_ids": [previous["sample_id"], current["sample_id"]], "limitations": LIMITATIONS}
