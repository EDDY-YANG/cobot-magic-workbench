"""Read-only admission of a separately staged NO_SEND runtime; no actuator."""
from pathlib import Path
import hashlib
import json
import os
import socket
import sys

SCHEMA = 'tiger-isolated-v2-nosend-binding-v1'


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_files(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): sha(p)
            for folder in ('src', 'vendor', 'scripts')
            for p in sorted((root / folder).rglob('*'))
            if p.is_file() and p.suffix in ('.py', '.json') and '__pycache__' not in p.parts}


def verify_files(root, hashes):
    root = Path(root).resolve(strict=True)
    require(isinstance(hashes, dict) and hashes, 'Nonempty frozen source manifest required')
    for rel, expected in hashes.items():
        path = root / rel
        require(not Path(rel).is_absolute() and '..' not in Path(rel).parts,
                'Manifest path escapes root')
        require(path.resolve(strict=True).is_relative_to(root) and not path.is_symlink(),
                'Manifest symlink/escape refused')
        require(sha(path) == expected, 'Frozen source changed: ' + rel)


def can_counters(base=Path('/sys/class/net')):
    result = {}
    for name in ('can_right', 'can_left'):
        path = base / name
        result[name] = {'ifindex': int((path / 'ifindex').read_text()),
                        'tx_packets': int((path / 'statistics/tx_packets').read_text())}
    return result


def verify_zero_tx(before, after):
    require(set(before) == set(after) == {'can_right', 'can_left'}, 'Both CAN counters required')
    for name in before:
        require(before[name]['ifindex'] == after[name]['ifindex'], 'CAN interface replaced')
        require(before[name]['tx_packets'] == after[name]['tx_packets'], 'CAN TX changed during NO_SEND')
    return {'right_TX_delta': 0, 'left_TX_delta': 0, 'interface_identity_unchanged': True}


def load_binding(path, expected_sha, runtime_root, *, physical=False):
    """Called only for explicitly requested online operation, never inspect."""
    require(sys.platform == 'linux', 'Real I/O entry requires the staged AgileX Linux host')
    require(len(expected_sha) == 64 and sha(path) == expected_sha, 'Binding SHA mismatch')
    binding = json.loads(Path(path).read_text(encoding='utf8'))
    require(binding.get('schema') == (SCHEMA if not physical else 'tiger-isolated-v2-physical-prepared-v1'), 'Unsupported runtime binding')
    require(binding.get('APPLY_supported') is physical, 'Runtime route/binding differs')
    require(socket.gethostname() == binding['host'], 'Wrong robot host')
    require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() == binding['boot_id'],
            'Host rebooted; capture a new binding without editing old evidence')
    require(Path(sys.executable).absolute() == Path(binding['python']).absolute(),
            'Use the exact independent interpreter')
    require(Path(sys.prefix) == Path(binding['python']).parents[1], 'Independent environment prefix differs')
    venv_config = Path(sys.prefix) / 'pyvenv.cfg'
    if venv_config.exists():
        require('include-system-site-packages = false' in venv_config.read_text().lower(),
                'Do not inherit production site-packages')
    root = Path(runtime_root).resolve(strict=True)
    require(root == Path(binding['runtime_root']).resolve(strict=True), 'Wrong independent runtime')
    require(Path(binding['python']).absolute().is_relative_to(root), 'Interpreter must be isolated in runtime')
    require(source_files(root) == binding['runtime_sources'], 'Runtime source set/SHA changed')
    canonical = Path(binding['canonical_root'])
    verify_files(canonical, binding['canonical_sources'])
    require({p.relative_to(canonical).as_posix() for p in (canonical/'agilex_control').rglob('*.py')}
            == set(binding['canonical_sources']), 'Canonical Python file set changed')
    verify_files(binding['schema_root'], binding['schema_sources'])
    require(sha(binding['config_path']) == binding['config_sha256'], 'Production configuration changed')
    cfg = json.loads(Path(binding['config_path']).read_text(encoding='utf8'))
    require(Path(cfg['remote_bundle']) == canonical, 'Do not rebase the existing Hub configuration')
    require(cfg.get('usb_selection') and cfg['usb_uuid'] == binding['usb_uuid'], 'Selected USB identity absent')
    require(Path(cfg['usb']) == Path(binding['usb_mount']), 'USB mount differs')
    require(root.is_relative_to(Path(cfg['usb'])), 'Runtime must reside on selected robot USB')
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(canonical), str(binding['schema_root'])]
    from agilex_control import storage
    require(Path(storage.__file__).resolve().is_relative_to(canonical.resolve()), 'Foreign canonical package')
    storage.verify_usb(cfg)
    require(os.stat(root).st_dev == os.stat(cfg['usb']).st_dev, 'Runtime is not on selected filesystem')
    require(f'{os.major(os.stat(root).st_dev)}:{os.minor(os.stat(root).st_dev)}' == binding['major_minor'],
            'USB device changed; new binding required')
    # Process-local dependency settings; never touch shell profile or services.
    acados = Path(binding['acados_source']).resolve(strict=True)
    require(acados.is_relative_to(root), 'acados source must belong to this independent runtime')
    os.environ['ACADOS_SOURCE_DIR'] = str(acados)
    require(os.environ.get('LD_LIBRARY_PATH') == str(acados/'lib'),
            'Use entry.py launcher to set dynamic-library path before Python startup')
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[name] = '1'
    return binding, cfg, storage


def check_stop(cfg, stop_file):
    from agilex_control.storage import require_not_stopped
    require_not_stopped(cfg)
    require(not Path(stop_file).exists(), 'Independent V2 stop requested')


def verify_model(binding, receipt):
    expected = binding['model']
    require(receipt['precision'] == expected['precision'] and receipt['step'] == expected['step'],
            'Currently loaded precision/checkpoint differs; no switching allowed')
    require(receipt['identity_sha256'] == expected['identity_sha256'], 'Loaded model contract changed')
    require(receipt.get('service_nonce_sha256') == expected.get('service_nonce_sha256'),
            'Model service instance changed')
    require(receipt.get('instance') == expected.get('instance'),
            'Model selection/worker instance changed; new route acceptance required')
