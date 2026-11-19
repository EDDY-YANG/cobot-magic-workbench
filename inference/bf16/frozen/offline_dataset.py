"""Strict, CPU-only decoder for the frozen real historical input collection."""
from pathlib import Path
import base64
import hashlib
import importlib.util
import json

EXPECTED_MANIFEST_SHA = '5b180d9c8342923dcb23e5e74c4b3654ec121d203835561a366b3efc0fc168ca'
EXPECTED_FROZEN = {
    'fp32_loader.py': 'a1024dc360b3e79bcfde21a33f2badbf5bbdd114d2a8379dbd943a999e882ba4',
    'fp32_http.py': '89aef2c1e4c168b30c84a5c77e70b0717299ef16ecae9d1f280887e6e7f8891f',
    'frozen_rtc_core.py': '5f2a4f959f3d72991d04bda0c6198861636bb6f7022dfa33dd26f4f10d8759e4',
}


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_module(name, path):
    import sys
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify_frozen_sources(experiment):
    frozen = Path(experiment) / 'frozen_source'
    for name, expected in EXPECTED_FROZEN.items():
        if sha256_file(frozen / name) != expected:
            raise ValueError('Frozen source SHA mismatch: '+name)


def load_dataset(root):
    root = Path(root).resolve(strict=True)
    manifest_path = root / 'dataset-manifest.json'
    if sha256_file(manifest_path) != EXPECTED_MANIFEST_SHA:
        raise ValueError('Dataset differs from independently frozen transfer manifest')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if manifest['schema'] != 'tiger-real-history-offline-v1' or len(manifest['samples']) != 16:
        raise ValueError('Expected the independently frozen 16-input real dataset')
    model=manifest['model']
    if (model['checkpoint_step']!=5000 or model['source_commit']!='215abfb217dbac7d5f1273282331b9b1866c0479'
            or model['normalization_sha256']!='22704c461f2485b8f15ff3b0aa99f4491e04c97735d53d1ca2e70f45dda85126'
            or model['tokenizer_sha256']!='8986bb4f423f07f8c7f70d0dbe3526fb2316056c17bae71b1ea975e77a168fc6'):
        raise ValueError('Historical checkpoint/norm/tokenizer/source contract differs')
    experiment = root.parent
    verify_frozen_sources(experiment)
    frozen = experiment / 'frozen_source'
    native_http = load_module('tiger_offline_frozen_decoder', frozen / 'fp32_http.py')
    rtc_core = load_module('tiger_offline_frozen_rtc_validation', frozen / 'frozen_rtc_core.py')
    import numpy as np
    samples = []
    ids = set();indices=set()
    for row in manifest['samples']:
        if row['sample_id'] in ids:
            raise ValueError('Duplicate historical sample identity')
        ids.add(row['sample_id'])
        if row['historical_request_index'] in indices:raise ValueError('Duplicate historical request index')
        indices.add(row['historical_request_index'])
        path = (root / row['input_file']).resolve(strict=True)
        path.relative_to(root)
        if sha256_file(path) != row['input_sha256']:
            raise ValueError('Historical input SHA mismatch: '+row['sample_id'])
        golden_path = (root / row['golden_file']).resolve(strict=True)
        golden_path.relative_to(root)
        if sha256_file(golden_path) != row['golden_sha256']:
            raise ValueError('Historical response SHA mismatch')
        saved = json.loads(path.read_text())
        snapshot = saved['snapshot']
        if saved['checkpoint_step'] != 5000 or snapshot['hardware_synchronized'] is not False:
            raise ValueError('Wrong historical identity or unsupported synchronization claim')
        frames = snapshot['frames']
        if set(frames) != {'front', 'right'}:
            raise ValueError('Exactly the two real views must be present')
        for role in ('front', 'right'):
            jpeg = base64.b64decode(frames[role]['jpeg_base64'], validate=True)
            if hashlib.sha256(jpeg).hexdigest() != frames[role]['sha256']:
                raise ValueError('Historical JPEG SHA mismatch: '+role)
        rtc_core.validate_rtc(saved['rtc'])
        if (saved['rtc']['trial_index'] != row['historical_trial_index']
                or saved['rtc']['request_index'] != row['historical_request_index']
                or row['inference_phase'] != ('RTC_CONTINUATION' if saved['rtc']['enabled'] else 'NATIVE_BOOTSTRAP')):
            raise ValueError('Historical sample/RTC identity mismatch')
        prefix_sha = hashlib.sha256(json.dumps(saved['rtc']['previous_actions'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if prefix_sha != row['previous_actions_sha256']:
            raise ValueError('Historical accepted prefix identity mismatch')
        observation = native_http.decode_observation({
            'state': snapshot['state_right_rad_m'],
            'front_jpeg_base64': frames['front']['jpeg_base64'],
            'right_jpeg_base64': frames['right']['jpeg_base64'],
            'prompt': saved['prompt'],
        })
        observation['rtc'] = saved['rtc']
        golden_data = json.loads(golden_path.read_text())
        if (golden_data['source_request_id'] != saved['request_id']
                or golden_data['action_dtype']!='float32' or golden_data['action_shape']!=[50,7]):
            raise ValueError('Historical response/request mismatch')
        golden = np.asarray(golden_data['actions'], dtype=np.float32)
        if golden.shape != (50, 7) or not np.isfinite(golden).all():
            raise ValueError('Invalid frozen historical response')
        samples.append((row, observation, golden))
    return manifest, sha256_file(manifest_path), samples


def validate_cpu(root):
    manifest, digest, samples = load_dataset(root)
    return {'status': 'REAL_INPUT_CPU_DECODE_AND_HASH_VERIFIED_NO_MODEL_RUN',
            'dataset_manifest_sha256': digest, 'sample_count': len(samples),
            'real_RGB_images': len(samples)*2,
            'native_samples': sum(not value['rtc']['enabled'] for _, value, _ in samples),
            'guided_RTC_samples': sum(value['rtc']['enabled'] for _, value, _ in samples),
            'observed_RGB_shapes': sorted({str(value[role].shape) for _, value, _ in samples for role in ('front','right')}),
            'state_shape': [7], 'raw_response_shape': [50,7],
            'planned_seed_pairs': len(samples)*len(manifest['seed_variants']),
            'GPU_model_calls': 0, 'robot_commands': 0,
            'scope': 'hash/decoder/RTC schema validation only; no model output validation'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate_cpu(args.dataset), ensure_ascii=False, indent=2))
