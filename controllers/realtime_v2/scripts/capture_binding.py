"""Capture a new real Linux/USB/model binding after independent staging.

No model launch, inference, recording or hardware command. This script is not
run as part of PC inspection/tests, and does not grant APPLY admission.
"""
from pathlib import Path
import argparse
import ast
import json
import math
import os
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from src.runtime_guard import SCHEMA, source_files, sha, require
from src.live_io import PinnedLoadedModel, owned_session
from src.v2_tracking_guard import tracking_policy
from src.window_contract import timing_policy
from src.joint_boundary_repair import policy as joint_reference_repair_policy
from src.bridge_step_policy import policy as bridge_step_policy


def documented_bounds(canonical, cfg):
    tree = ast.parse((canonical/'agilex_control/hardware.py').read_text(encoding='utf8'))
    found = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'DOCUMENTED_COMMAND_LIMITS_DEG' for t in node.targets)]
    require(len(found) == 1 and len(found[0]) == 6, 'Documented six-axis position limits unavailable')
    limits = [[math.radians(float(a)), math.radians(float(b))] for a, b in found[0]]
    limits.append(list(cfg['gripper_bounds_m']))
    require(all(math.isfinite(a) and math.isfinite(b) and a < b for a, b in limits), 'Invalid position limits')
    return limits


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute-readonly', action='store_true')
    p.add_argument('--operator-skip-no-motion-acceptance', action='store_true')
    p.add_argument('--canonical-root', type=Path, required=True)
    p.add_argument('--schema-root', type=Path, required=True)
    p.add_argument('--config-path', type=Path, required=True)
    p.add_argument('--precision', choices=('fp32', 'bf16'), required=True)
    p.add_argument('--step', type=int, choices=(2000, 5000, 10000, 15000, 20000), required=True)
    p.add_argument('--acados-source', type=Path, required=True)
    p.add_argument('--solver-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    require(a.execute_readonly and sys.platform == 'linux', 'Explicit staged Linux read-only binding required')
    require(Path(sys.executable).absolute().is_relative_to(ROOT) and Path(sys.prefix) == Path(sys.executable).parents[1],
            'Use the new runtime independent Python')
    cfg = json.loads(a.config_path.read_text(encoding='utf8'))
    require(Path(cfg['remote_bundle']) == a.canonical_root and cfg.get('usb_selection'), 'Real selected config required')
    require(ROOT.is_relative_to(Path(cfg['usb'])), 'Runtime must be on actual selected robot USB')
    require(a.output.parent.is_dir() and a.output.is_relative_to(ROOT) and not a.output.exists(), 'New own binding output required')
    require(a.acados_source.resolve().is_relative_to(ROOT) and a.solver_dir.resolve().is_relative_to(ROOT),
            'Independent acados/build directories required')
    sys.path[:0] = [str(a.canonical_root), str(a.schema_root)]
    from agilex_control import storage
    from agilex_control.h50_journal import H50USBJournal
    canonical_sources = {f.relative_to(a.canonical_root).as_posix(): sha(f)
                         for f in sorted((a.canonical_root/'agilex_control').rglob('*.py'))}
    schema_sources = {f.relative_to(a.schema_root).as_posix(): sha(f)
                      for folder in ('bf16_service_contract', 'frozen_rtc')
                      for f in sorted((a.schema_root/folder).rglob('*.py'))}
    with owned_session(cfg, storage=storage):
        storage.require_not_stopped(cfg)
        current = PinnedLoadedModel(a.precision, a.step).bind()
        from src.temporal_rpc import TimeQPHTTP, SCHEMA as TIMEQP_SCHEMA, OFFICIAL_SHA, implementation_sha256
        timeqp = TimeQPHTTP().request('GET', '/health')
        require(timeqp.get('ready') is True and timeqp.get('schema') == TIMEQP_SCHEMA
                and timeqp.get('official_sha256') == OFFICIAL_SHA
                and timeqp.get('implementation_sha256') == implementation_sha256(), 'Independent server TimeQP version not ready')
        device = os.stat(cfg['usb']).st_dev
        binding = {'schema': SCHEMA, 'APPLY_supported': False,
            'preparation_basis': ('FRESH_IDENTITY_ONLY_OPERATOR_SKIPPED_NO_SEND'
                                  if a.operator_skip_no_motion_acceptance else 'FRESH_IDENTITY_FOR_NO_SEND'),
            'no_send_validated_by_this_capture': False,
            'host': socket.gethostname(), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'runtime_root': str(ROOT), 'python': str(Path(sys.executable).absolute()),
            'canonical_root': str(a.canonical_root), 'canonical_sources': canonical_sources,
            'schema_root': str(a.schema_root), 'schema_sources': schema_sources,
            'config_path': str(a.config_path), 'config_sha256': sha(a.config_path),
            'runtime_sources': source_files(ROOT), 'usb_mount': cfg['usb'], 'usb_uuid': cfg['usb_uuid'],
            'major_minor': f'{os.major(device)}:{os.minor(device)}',
            'acados_source': str(a.acados_source), 'solver_dir': str(a.solver_dir),
            'bounds_rad_m': documented_bounds(a.canonical_root, cfg),
            'bounds_source': 'SDK_DOCUMENTED_ONLY_NO_CONTROLLER_QUERY_NO_SEND',
            'model': {k: current.get(k) for k in ('precision','step','identity_sha256','service_nonce_sha256','instance')},
            'timeqp': timeqp, 'tracking_policy': tracking_policy(), 'timing_policy': timing_policy(),
            'joint_reference_repair_policy':joint_reference_repair_policy(),
            'bridge_step_policy':bridge_step_policy(),
            'supported_window_s': {'min':1., 'max':1800., 'default':40., 'acceptance':30.},
            'model_calls': 0, 'robot_commands': 0, 'model_selected_or_changed': False}
        journal = H50USBJournal(cfg, ROOT)
        try:
            journal.write_json(a.output, binding)
        finally:
            journal.close()
    print(json.dumps({'binding': str(a.output), 'sha256': sha(a.output), 'APPLY_supported': False}))


if __name__ == '__main__':
    main()
