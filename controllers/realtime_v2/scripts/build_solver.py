"""Prebuild/warm the genuine CPU solver before acquiring live observations.

Example (Linux, separate environment already provisioned):
  python scripts/build_solver.py --bounds-json contract.json --solver-dir build/mpc
contract.json must contain bounds_rad_m[7,2]. No hardware/model imports.
ACADOS_SOURCE_DIR and the platform runtime library search path must be set.
"""
from pathlib import Path
import argparse
import json
import sys
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from v2_core import V2Core, SOURCE_MANIFEST, BUILD_RECEIPTS

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bounds-json', required=True)
    parser.add_argument('--solver-dir', required=True)
    parser.add_argument('--receipt')
    args = parser.parse_args()
    doc = json.loads(Path(args.bounds_json).read_text(encoding='utf8'))
    bounds = np.asarray(doc['bounds_rad_m'], float)
    if bounds.shape != (7, 2):
        raise ValueError('bounds_rad_m must be7x2')
    # Explicit build-only synthetic anchor; no real observation is claimed.
    anchor = bounds.mean(axis=1)
    start = time.monotonic_ns()
    core = V2Core(anchor, bounds, start, clock_domain=('CPU_BUILD', 'BUILD_ONLY', 'MONOTONIC'),
                  solver_dir=args.solver_dir, allow_solver_build=True)
    rows = [core.tick(start+i*20_000_000, anchor, start+i*20_000_000) for i in range(10)]
    result = {'status': 'REAL_ACADOS_BUILD_AND_SYNTHETIC_WARMUP_PASSED',
        'source_manifest': SOURCE_MANIFEST, 'builds': BUILD_RECEIPTS,
        'solver_calls': len(rows), 'physical_dispatch_count': 0, 'model_calls': 0,
        'scope': 'synthetic CPU build fixture, not live feedback or model acceptance'}
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
