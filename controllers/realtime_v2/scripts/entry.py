"""Default inspect is offline. Online modes require a real staged binding."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', default='inspect', choices=('inspect', 'compare-first', 'continuous', 'stop', 'reset-stop'))
    parser.add_argument('--execute-online', action='store_true')
    parser.add_argument('--binding', type=Path)
    parser.add_argument('--binding-sha256')
    parser.add_argument('--prompt', default='右臂将红色方块放入绿色桶')
    parser.add_argument('--prompt-base64', help='UTF-8 prompt transport for PC PowerShell')
    parser.add_argument('--duration', type=float, default=30.)
    parser.add_argument('--trial-index', type=int, default=0)
    parser.add_argument('--bootstrap', choices=('native_first', 'guided_hold35'), default='native_first')
    parser.add_argument('--gripper-lookahead-ms', type=float, choices=(0., 90.), default=0.)
    parser.add_argument('--stop-on-stdin-eof', action='store_true')
    parser.add_argument('--runtime-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.prompt_base64:
        try:
            args.prompt = base64.b64decode(args.prompt_base64, validate=True).decode('utf8')
        except (ValueError, UnicodeError):
            parser.error('Invalid UTF-8 prompt transport')
    if args.mode == 'inspect':
        print(json.dumps({'status': 'OFFLINE_INSPECTION_ONLY', 'profile': 'isolated-official-v2-nosend-v1',
            'root': str(ROOT), 'H50': 50, 'guided_RTC': True, 'reference_hz': 18, 'control_hz': 50,
            'model_calls': 0, 'hardware_calls': 0, 'remote_connections': 0, 'APPLY_supported': False,
            'real_NO_SEND_verified': False, 'online_binding_required': True,
            'gripper_default': 'common-time-axis VLA opening, no added A0.1 smoothing',
            'bootstrap_modes': ['native_first', 'guided_hold35']}, ensure_ascii=False))
        return 0
    if not args.execute_online or not args.binding or not args.binding_sha256:
        parser.error('Online/stop requires --execute-online, --binding and --binding-sha256')
    if sys.platform != 'linux':
        parser.error('Online entry only runs on the staged AgileX Linux host')
    if not (1 <= args.duration <= 60 and 0 <= args.trial_index <= 1_000_000):
        parser.error('Duration 1..60s and bounded nonnegative trial index required')
    if not args.prompt.strip() or len(args.prompt) > 200 or '\n' in args.prompt:
        parser.error('Explicit single-line prompt, 1..200 characters required')
    raw = args.binding.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.binding_sha256:
        parser.error('Binding file SHA differs')
    binding = json.loads(raw)
    # Set loader path before re-exec, not after importing acados in a live loop.
    if not args.runtime_child:
        python = Path(binding['python']).absolute()
        acados = Path(binding['acados_source']).absolute()
        if ROOT not in python.parents or ROOT not in acados.parents:
            parser.error('Environment and acados must belong to this isolated directory')
        env = dict(os.environ)
        env['ACADOS_SOURCE_DIR'] = str(acados)
        env['LD_LIBRARY_PATH'] = str(acados/'lib')
        env['PYTHONNOUSERSITE'] = '1'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env.pop('PYTHONPATH', None)
        for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            env[key] = '1'
        os.execve(str(python), [str(python), '-I', '-B', str(Path(__file__).resolve()),
                  *(sys.argv[1:] if argv is None else argv), '--runtime-child'], env)
    from src.runtime_guard import load_binding
    binding, cfg, storage = load_binding(args.binding, args.binding_sha256, ROOT)
    if args.mode == 'reset-stop':
        from src.live_io import owned_session
        with owned_session(cfg, storage=storage):
            marker = ROOT/'STOP-NO_SEND'
            if marker.exists():
                if marker.is_symlink() or marker.resolve().parent != ROOT:
                    parser.error('Invalid independent stop marker')
                marker.unlink()
            storage.verify_usb(cfg)
        print(json.dumps({'status': 'INDEPENDENT_STOP_RESET', 'canonical_STOP_changed': False,
                          'model_calls': 0, 'robot_commands': 0}))
        return 0
    if args.mode == 'stop':
        # Stop only this runtime. No canonical STOP, service stop or process kill.
        from agilex_control.h50_journal import H50USBJournal
        journal = H50USBJournal(cfg, ROOT)
        try:
            journal.write_json(ROOT/'STOP-NO_SEND', {'stop': True, 'scope': str(ROOT), 'robot_command': False})
        finally:
            journal.close()
        print(json.dumps({'status': 'INDEPENDENT_STOP_REQUESTED', 'drain_still_required': True,
                          'physical_stop_performed': False}))
        return 0
    from src.runtime import execute
    execute(binding, cfg, storage, args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
