"""One bounded local CPU suite; preserves failures and exact code hashes."""
from pathlib import Path
import datetime
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--acados-source', type=Path, default=Path('D:/2026-10-22-13-47-V2-acados-CPU/acados') if os.name=='nt' else None)
parser.add_argument('--toolchain-bin', type=Path, default=Path('D:/2026-10-22-13-47-V2-acados-CPU/w64devkit/bin') if os.name=='nt' else None)
parser.add_argument('--solver-dir', type=Path, default=Path('D:/2026-10-23-11-24-V2-NOSEND-CPU/generated') if os.name=='nt' else ROOT/'build/test_solver')
args = parser.parse_args()
env = dict(os.environ)
if args.acados_source:
    if not args.acados_source.is_dir():
        parser.error('The existing independent acados source is missing')
    env['ACADOS_SOURCE_DIR'] = str(args.acados_source)
if args.toolchain_bin:
    env['V2_CMAKE_BIN'] = env['V2_TOOLCHAIN_BIN'] = str(args.toolchain_bin)
env['V2_TEST_SOLVER_DIR'] = str(args.solver_dir)
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
    env[key] = '1'
stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d-%H-%M-%S')
target = ROOT/'receipts'/f'{stamp}-final-local-validation'
command = [sys.executable, '-X', 'utf8', '-I', '-B', '-m', 'unittest', 'discover', '-s', str(ROOT/'tests'), '-v']
started = time.perf_counter()
result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, encoding='utf8', errors='replace', timeout=300)
log = result.stdout + '\n' + result.stderr
target.with_suffix('.log').write_text(log, encoding='utf8')
matches = re.findall(r'Ran (\d+) tests?', log)
receipt = {'schema': 'tiger-v2-local-cpu-validation-v1', 'command': command,
    'returncode': result.returncode, 'tests': int(matches[-1]) if matches else None,
    'elapsed_s': time.perf_counter()-started, 'success': result.returncode == 0 and bool(matches),
    'independent_acados_source': env.get('ACADOS_SOURCE_DIR'), 'independent_solver_dir': str(args.solver_dir),
    'evidence_scope': 'REAL_ACADOS_CPU_PLUS_SYNTHETIC_IO_PROTOCOL_FIXTURES',
    'real_model_calls': 0, 'robot_commands': 0, 'SSH_calls': 0,
    'localhost_fixture_HTTP_only': True, 'real_NO_SEND_accepted': False,
    'sources_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ('src','scripts','tests') for p in sorted((ROOT/folder).rglob('*.py'))},
    'log': target.with_suffix('.log').name}
target.with_suffix('.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
print(json.dumps({k:v for k,v in receipt.items() if k != 'sources_sha256'}, ensure_ascii=False))
if result.returncode:
    print(log[-12000:])
raise SystemExit(result.returncode)
