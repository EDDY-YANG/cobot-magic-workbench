"""Reuse the SHA-pinned FP32 loader; only its receipt/output JOB is isolated."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import sys
import os
from pathlib import Path

OLD_JOB = Path('/opt/cobot-workbench/lab-packages/2026-10-19-15-38-Pi05-FP32-Deployment')
NEW_JOB = Path('/opt/cobot-workbench/lab-packages/2026-10-19-18-12-H50-Model-Supervisor')
LOADER = OLD_JOB / 'scripts/2026-10-19-15-38-fp32-loader-v3.py'
LOADER_SHA = 'a1024dc360b3e79bcfde21a33f2badbf5bbdd114d2a8379dbd943a999e882ba4'
HTTP_SHA = '89aef2c1e4c168b30c84a5c77e70b0717299ef16ecae9d1f280887e6e7f8891f'
ENTRY_SHA_AT_LOAD = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--step', type=int, choices=(2000, 5000, 10000, 15000, 20000), required=True)
    parser.add_argument('--port', type=int, choices=(18884,), required=True)
    args = parser.parse_args()
    for path, expected in ((LOADER, LOADER_SHA), (OLD_JOB / 'scripts/fp32_http.py', HTTP_SHA)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Frozen FP32 source identity mismatch')
    if (NEW_JOB / 'source').resolve(strict=True) != (OLD_JOB / 'source').resolve(strict=True):
        raise RuntimeError('Frozen source pointer mismatch')
    import fcntl
    # Same advisory owner as the original loader, plus the isolated JOB's lock.
    # An old compute owner is preserved; the loader also checks nvidia-smi.
    lock = (OLD_JOB / 'FP32_GPU_OWNER.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    spec = importlib.util.spec_from_file_location('tiger_original_fp32_loader', LOADER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.JOB = NEW_JOB
    module.guard()
    own_lock = (NEW_JOB / 'FP32_GPU_OWNER.lock').open('a+')
    fcntl.flock(own_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run_id = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M-%S') + '-step%d-worker-%d' % (args.step, os.getpid())
    receipts = NEW_JOB / 'receipts' / run_id
    receipts.mkdir()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gpu_inventory import require_available
    inventory = require_available(receipts / 'GPU-owners-before-model.json')
    # Do not invoke the original main: it assumed the GPU process list was empty.
    # Original FP32Policy, transforms, restore, IR audit, RNG and inference remain intact.
    try:
        policy = module.FP32Policy(args.step, num_steps=10, seed=42)
        module.write_json(receipts / 'parameter-restoration.json', {
            'status': 'PARAMETERS_LOADED_FP32', 'step': args.step, 'pid': os.getpid(),
            'metadata': policy.metadata, 'parameter_summary': policy.param_after,
            'load_seconds': policy.load_seconds, 'original_loader_sha256': LOADER_SHA,
            'gpu_inventory_receipt': str(receipts / 'GPU-owners-before-model.json')})
        meta, sample = module.real_samples(1)[0]
        native_receipts, rtc_receipts = receipts / 'native', receipts / 'rtc'
        native_receipts.mkdir()
        rtc_receipts.mkdir()
        policy.audit_and_compile(sample, native_receipts)
        result = policy.infer(sample)
        output = NEW_JOB / 'outputs' / (run_id + '-' + meta['sample_id'] + '.npy')
        policy.np.save(output, result['actions'], allow_pickle=False)
        from rtc_core import RTCFP32Adapter, bootstrap
        rtc_policy = RTCFP32Adapter(policy)
        rtc_policy.source_identity = {
            'fp32_loader_sha256': LOADER_SHA, 'worker_entry_sha256': ENTRY_SHA_AT_LOAD,
            'supervisor_sha256': os.environ.get('TIGER_SUPERVISOR_SHA256') or
                hashlib.sha256((NEW_JOB / 'scripts/model_supervisor.py').read_bytes()).hexdigest()}
        rtc_policy.audit_and_compile({**sample, 'rtc': bootstrap()}, rtc_receipts)
        equality = rtc_policy.verify_zero_weight_equivalence(sample)
        for name, array in zip(('native', 'rtc'), rtc_policy.zero_weight_arrays):
            path = NEW_JOB / 'outputs' / (run_id + '-' + name + '-zero-equivalence.npy')
            policy.np.save(path, array, allow_pickle=False)
            equality[name + '_array_file'] = str(path)
            equality[name + '_array_file_sha256'] = module.sha(path)
        module.write_json(rtc_receipts / 'zero-weight-equivalence.json', equality)
        if equality['pass'] is not True:
            raise RuntimeError('Zero-weight native32 exact equivalence failed; original arrays and zero tolerance are retained')
        warm_rtc = rtc_policy.infer_rtc({**sample, 'rtc': bootstrap()})
        report = {'status': 'FP32_WORKER_REAL_INPUT_AND_RTC_IR_VERIFIED', 'step': args.step, 'pid': os.getpid(),
                  'metadata': policy.metadata, 'parameter_summary': policy.param_after,
                  'load_seconds': policy.load_seconds, 'compile_seconds': policy.compile_seconds,
                  'native_precision_audit': policy.audit, 'rtc_precision_audit': rtc_policy.audit,
                  'rtc': rtc_policy.rtc, 'zero_weight_equivalence': equality,
                  'historical_real_inputs': 1, 'native_warmup_inferences': 1,
                  'historical_input': meta, 'actions_file': str(output), 'actions_sha256': module.sha(output),
                  'native_timing': result['timing'], 'rtc_bootstrap_timing': warm_rtc['timing'], 'robot_commands': 0,
                  'candidate_selection': 'Explicit user-selected checkpoint; no quality ranking claim',
                  'rng_policy': 'RTC key=fold_in(key(42+trial_index),request_index); native compatibility retains split stream',
                  'gpu_inventory_receipt': str(receipts / 'GPU-owners-before-model.json')}
        module.write_json(receipts / 'validation.json', report)
        module.write_json(NEW_JOB / 'receipts/worker-current.json', {**report, 'receipts': str(receipts)})
        old_scripts = str(OLD_JOB / 'scripts')
        sys.path.insert(0, old_scripts)
        from fp32_rtc_http import serve
        serve(rtc_policy, args.port, ready_file=receipts / 'http-ready.json')
    except BaseException as exc:
        import traceback
        module.write_json(receipts / 'failure.json', {'status': 'FAILED_NO_PRECISION_FALLBACK',
                          'step': args.step, 'pid': os.getpid(), 'error': repr(exc),
                          'traceback': traceback.format_exc(), 'robot_commands': 0})
        raise


if __name__ == '__main__':
    main()
