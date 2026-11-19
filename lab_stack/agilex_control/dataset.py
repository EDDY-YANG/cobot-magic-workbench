# PC PowerShell：在正式项目根目录运行，不要单独运行本模块。
# .\2026-10-06-15-00-AgileX.cmd record-list
# 导出需使用真实 episode 路径并完成审阅；详见 README.md 与任务总表。
# 本模块只处理已有录制；不会开相机、CAN、模型或驱动机械臂。
"""Explicit native-review -> LeRobot0.5.2 right-arm dataset conversion.

No hardware access; no state-as-action substitution, gap filling, success label
invention or left-arm data in the right-arm feature vector. Run only after human
review. Original capture files stay untouched, including failures.
"""
import json
import copy
import hashlib
import math
from pathlib import Path
from .recording import iter_chunk
from .storage import verify_usb, write_json
from .collection import CollectionStore, validate_metadata
from .capture_profile import receipt_contract, ALL


def stream_files(source, channel):
    files = sorted(source.glob(channel + "-*.bin"))
    if not files:
        raise RuntimeError("Missing native channel: " + channel)
    for path in files:
        yield from iter_chunk(path)


def accepted_rows(source, max_age=.1, max_step=.05):
    """Streaming past-only image lookup, with true software sample times."""
    cursors, pending, latest, previous_camera_index = {}, {}, {}, {}
    for role in ("front", "right"):
        cursors[role] = iter(stream_files(source, role))
        pending[role] = next(cursors[role], None)
    previous = None
    for stamp, payload in stream_files(source, "state"):
        record = json.loads(payload)
        if record["sample_wall_ns"] != stamp:
            raise RuntimeError("State timestamp and chunk timestamp differ")
        if previous is not None and not 0 < (stamp - previous)/1e9 <= max_step:
            raise RuntimeError("Native sample discontinuity; no interpolation or frame deletion")
        previous = stamp
        arm = record["sensors"]["arms"]["right"]
        if (not arm["feedback_fresh"] or arm["q_deg"] is None or arm["grip_m"] is None
                or arm["age_joint_s"] is None or arm["age_joint_s"] > max_age):
            raise RuntimeError("Actual right-arm feedback missing/stale")
        action = arm["action_targets"]
        ages = [*action["pair_ages_s"], action["grip_age_s"]]
        if action["q_deg"] is None or action["grip_m"] is None or any(age is None or not 0 <= age <= max_age for age in ages):
            raise RuntimeError("Real bus-observed action targets missing/stale; feedback cannot replace them")
        source_index = record.get('camera_source_index_v1')
        if 'camera_source_index_v1' in record:
            if (not isinstance(source_index, dict)
                    or source_index.get('schema') != 'tiger-recorded-camera-source-index-v1'
                    or not isinstance(source_index.get('clock_domain'), dict)
                    or not source_index['clock_domain']
                    or source_index['clock_domain'].get('identity_verified') is not True
                    or not all(isinstance(source_index['clock_domain'].get(key), str) and source_index['clock_domain'][key]
                               for key in ('host_id', 'boot_id', 'session_id'))
                    or type(source_index.get('sample_monotonic_ns')) is not int
                    or source_index['sample_monotonic_ns'] < 0
                    or not isinstance(source_index.get('cameras'), dict)):
                raise RuntimeError('Recorded camera source index/domain invalid; no legacy fallback')
        frames, camera_ages = {}, {}
        for role in ("front", "right"):
            selected = None if source_index is None else source_index['cameras'].get(role)
            selected_stamp = stamp
            if source_index is not None:
                if (not isinstance(selected, dict) or type(selected.get('wall_ns')) is not int
                        or selected['wall_ns'] <= 0 or selected['wall_ns'] > stamp
                        or record.get('camera_sample_ns', {}).get(role) != selected['wall_ns']
                        or type(selected.get('frame_id')) not in (str, int)
                        or selected['frame_id'] in ('', None)):
                    raise RuntimeError('Recorded camera identity missing/invalid: ' + role)
                timing = selected.get('source_timing')
                available = selected.get('available_monotonic_ns')
                source_ns = timing.get('source_monotonic_ns_estimate') if isinstance(timing, dict) else None
                if (not isinstance(timing, dict) or timing.get('clock_domain') != source_index['clock_domain']
                        or type(available) is not int or type(source_ns) is not int
                        or not 0 <= source_ns <= available <= source_index['sample_monotonic_ns']
                        or ('available_monotonic_ns' in timing and timing['available_monotonic_ns'] != available)):
                    raise RuntimeError('Recorded camera availability/domain unverified: ' + role)
                previous_camera = previous_camera_index.get(role)
                if previous_camera is not None:
                    if (previous_camera['frame_id'] == selected['frame_id'] or previous_camera['wall_ns'] == selected['wall_ns']) and previous_camera != selected:
                        raise RuntimeError('Repeated camera identity changed source metadata: ' + role)
                    if selected['wall_ns'] < previous_camera['wall_ns']:
                        raise RuntimeError('Recorded camera identity moved backwards: ' + role)
                previous_camera_index[role] = copy.deepcopy(selected)
                selected_stamp = selected['wall_ns']
            # New records name the image captured in this state sample. Do not
            # substitute a later-available image whose source wall time is old.
            while pending[role] is not None and pending[role][0] <= selected_stamp:
                latest[role] = pending[role]
                pending[role] = next(cursors[role], None)
            if role not in latest:
                raise RuntimeError("No past image at first sample; no future backfill")
            image_stamp, jpg = latest[role]
            if source_index is not None and image_stamp != selected_stamp:
                raise RuntimeError('Recorded selected camera frame missing: ' + role)
            age = (stamp - image_stamp)/1e9
            if source_index is not None:
                age = (source_index['sample_monotonic_ns'] - selected['source_timing']['source_monotonic_ns_estimate']) / 1e9
            if not 0 <= age <= max_age:
                raise RuntimeError("Recorded image discontinuity: " + role)
            frames[role], camera_ages[role] = jpg, age
        state = [math.radians(x) for x in arm["q_deg"]] + [arm["grip_m"]]
        target = [math.radians(x) for x in action["q_deg"]] + [action["grip_m"]]
        if not all(math.isfinite(x) for x in state + target):
            raise RuntimeError("Nonfinite recorded sensor or action value")
        row = {"stamp_ns": stamp, "state": state, "action": target, "images": frames,
               "camera_age_s": [camera_ages["front"], camera_ages["right"]], "joint_age_s": arm["age_joint_s"]}
        if source_index is not None:
            row['native_source_timing'] = {'camera_source_index_v1': copy.deepcopy(source_index),
                'arm_source_timing': copy.deepcopy(arm.get('source_timing'))}
        yield row


def _collection_review(cfg, source, receipt, receipt_sha256):
    """Check persisted human acceptance against the exact receipt being exported."""
    metadata = validate_metadata(receipt['collection_v1'])
    evidence = CollectionStore(cfg).require_accepted_episode(metadata['batch_id'], source.name)
    if (evidence['path'] != str(source) or evidence['metadata'] != metadata
            or evidence['receipt_sha256'] != receipt_sha256):
        raise RuntimeError('Collection review does not match the exact native source receipt')
    return evidence


def export_right(cfg, root, episode, reviewed=False):
    verify_usb(cfg)
    source = Path(episode).resolve(strict=True)
    if not source.is_relative_to((root / "runs").resolve()):
        raise RuntimeError("Source must be this library's original native episode on verified USB")
    receipt_bytes = (source / 'operation.json').read_bytes()
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    receipt = json.loads(receipt_bytes.decode('utf-8'))
    is_collection = 'collection_v1' in receipt
    if not is_collection and not reviewed:
        raise RuntimeError("Human episode review is required; do not infer task success from file validity")
    if not is_collection:
        binding = CollectionStore(cfg).lookup_episode_binding(source.name, str(source))
        if binding is not None:
            raise RuntimeError('Bound collection metadata is missing; --reviewed cannot override its receipt SHA review')
    frozen_capture = receipt_contract(cfg, receipt)
    if frozen_capture['profile'] != ALL and not is_collection:
        raise RuntimeError('Scoped episodes require a SHA-bound collection human review; --reviewed cannot override it')
    if (receipt.get("format") != "tiger-native-sensors-v1"
            or receipt.get("status") != "RECORDED_NATIVE_REVIEW_REQUIRES_QC"
            or receipt.get("failure") or receipt.get("queue_overflows")):
        raise RuntimeError("Incomplete/wrong native episode format; preserve for review")
    collection_review = _collection_review(cfg, source, receipt, receipt_sha256) if is_collection else None
    # Validate all rows before making a dataset. Two streaming passes preserve
    # predictable RAM use and explicitly reject gaps before export creation.
    count, timed_count = 0, 0
    for row in accepted_rows(source):
        count += 1
        timed_count += 'native_source_timing' in row
    if count < 2:
        raise RuntimeError("Too few complete real samples")
    if is_collection:
        collection_review = _collection_review(cfg, source, receipt, receipt_sha256)
    import importlib.metadata
    import cv2
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    if importlib.metadata.version("lerobot") != "0.5.2":
        raise RuntimeError("Export requires LeRobot0.5.2")
    destination = root / "exports" / (source.name + "-right-lerobot")
    if destination.exists():
        raise RuntimeError("Export destination exists; no overwrite")
    names = ["J1_rad", "J2_rad", "J3_rad", "J4_rad", "J5_rad", "J6_rad", "gripper_m"]
    features = {"observation.state": {"dtype": "float32", "shape": (7,), "names": names},
                "action": {"dtype": "float32", "shape": (7,), "names": names},
                "source.timestamp_ns": {"dtype": "int64", "shape": (1,), "names": ["software_wall_ns"]},
                "source.camera_age_s": {"dtype": "float32", "shape": (2,), "names": ["front", "right"]},
                "source.joint_age_s": {"dtype": "float32", "shape": (1,), "names": ["right_joint"]}}
    for role in ("front", "right"):
        features["observation.images." + role] = {"dtype": "video", "shape": (480, 640, 3),
                                                   "names": ["height", "width", "channels"]}
    dataset = LeRobotDataset.create(repo_id="tiger/" + source.name, fps=30, root=destination,
        robot_type="piper_right", features=features, use_videos=True, image_writer_processes=0, image_writer_threads=4)
    exported_count = 0
    for row in accepted_rows(source):
        verify_usb(cfg)
        frame = {"observation.state": np.asarray(row["state"], dtype=np.float32),
                 "action": np.asarray(row["action"], dtype=np.float32), "task": receipt["task"],
                 "source.timestamp_ns": np.asarray([row["stamp_ns"]], dtype=np.int64),
                 "source.camera_age_s": np.asarray(row["camera_age_s"], dtype=np.float32),
                 "source.joint_age_s": np.asarray([row["joint_age_s"]], dtype=np.float32)}
        for role, jpeg in row["images"].items():
            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape != (480, 640, 3):
                raise RuntimeError("Actual recorded JPEG decode/profile failed")
            frame["observation.images." + role] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        dataset.add_frame(frame)
        exported_count += 1
    if exported_count != count:
        raise RuntimeError('Native sample count changed between validation and conversion; no success receipt')
    dataset.save_episode()
    dataset.finalize()
    result = {"native_source": str(source), "export": str(destination), "frames": count,
              "capture_contract": frozen_capture,
              "left_features_included": False, "interpolation": False,
              "action_basis": "bus-observed command targets", "task_success": "human_review_only",
              "fps_metadata": 30, "actual_software_timestamps_retained": True}
    result['human_review'] = {'basis': 'sha_bound_collection_review' if is_collection else 'legacy_reviewed_flag',
                             'native_receipt_sha256': receipt_sha256,
                             'collection_evidence': collection_review,
                             'snapshot_only': True,
                             'current_collection_review_required_before_training': is_collection}
    result['export_completed'] = True
    result['training_export_verified'] = False  # Conversion is separate from training acceptance.
    # Full nested provenance remains in the original immutable native state
    # chunks. Bind those exact files in the export receipt without relabelling
    # old episodes or inserting metadata into the model's state/action vectors.
    result['source_timing'] = {'indexed_frames': timed_count, 'legacy_frames': count - timed_count,
        'schema': 'tiger-recorded-camera-source-index-v1' if timed_count else None,
        'common_observation_alignment_verified': False,
        'metadata_location': 'native state.camera_source_index_v1 and state.sensors.arms.right.source_timing',
        'row_lookup': 'source.timestamp_ns equals native state chunk/sample_wall_ns',
        'requires_original_native_state_files': True,
        'native_state_files': []}
    for path in sorted(source.glob('state-*.bin')):
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        result['source_timing']['native_state_files'].append({'path': str(path), 'sha256': digest.hexdigest()})
    if is_collection:
        # A human can revoke review or a receipt can change during conversion.
        # Preserve partial export files on failure; do not publish a success receipt.
        result['human_review']['collection_evidence'] = _collection_review(cfg, source, receipt, receipt_sha256)
    write_json(destination / "2026-10-06-14-05-export-receipt.json", result, cfg)
    return result
