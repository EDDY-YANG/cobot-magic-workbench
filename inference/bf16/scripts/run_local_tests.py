"""Run local CPU protocol tests with external network/model imports denied."""
from pathlib import Path
import argparse
import datetime
import hashlib
import importlib.abc
import ipaddress
import json
import os
import re
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]


def install_guard():
    events={'external_connection_attempts':[], 'blocked_process_attempts':[],
            'blocked_model_imports':[], 'loopback_connections':0, 'local_python_children':0}
    denied={'jax','jaxlib','torch','flax','openpi','transformers','piper_sdk','pyrealsense2','serial','rclpy'}
    class NoModelImports(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,path=None,target=None):
            if fullname.split('.')[0] in denied:
                events['blocked_model_imports'].append(fullname)
                raise RuntimeError('OFFLINE_TEST_MODEL_IMPORT_FORBIDDEN: '+fullname)
    sys.meta_path.insert(0,NoModelImports())
    def local(host):
        try:return ipaddress.ip_address(str(host)).is_loopback
        except ValueError:return host in ('localhost',None)
    def audit(event,args):
        if event in ('socket.connect','socket.bind'):
            address=args[1]
            if isinstance(address,tuple):
                if not local(address[0]):
                    events['external_connection_attempts'].append(str(address[0]))
                    raise RuntimeError('OFFLINE_TEST_EXTERNAL_NETWORK_FORBIDDEN')
                if event=='socket.connect':events['loopback_connections']+=1
        elif event=='socket.getaddrinfo' and not local(args[0]):
            events['external_connection_attempts'].append(str(args[0]))
            raise RuntimeError('OFFLINE_TEST_EXTERNAL_DNS_FORBIDDEN')
        elif event=='subprocess.Popen':
            executable=args[0]
            if executable is None:
                command=args[1]
                if isinstance(command,(list,tuple)):executable=command[0]
                else:
                    match=re.match(r'^\s*(?:"([^"]+)"|(\S+))',str(command))
                    executable=next((x for x in match.groups() if x),None) if match else None
            if Path(str(executable)).name.lower()=='lscpu':return
            if os.path.normcase(str(executable))!=os.path.normcase(sys.executable) or '--execute-online' in str(args[1]):
                events['blocked_process_attempts'].append(str(executable))
                raise RuntimeError('OFFLINE_TEST_EXTERNAL_PROCESS_FORBIDDEN')
            events['local_python_children']+=1
        elif event=='os.system':raise RuntimeError('OFFLINE_TEST_SHELL_FORBIDDEN')
        elif event=='exec' and Path(args[0].co_filename).name in ('mixed_policy.py','worker_entry.py','fp32_loader.py'):
            events['blocked_model_imports'].append(args[0].co_filename)
            raise RuntimeError('OFFLINE_TEST_REAL_LOADER_EXEC_FORBIDDEN')
    sys.addaudithook(audit)
    return events


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--pattern',default='test_*.py');args=parser.parse_args()
    if args.receipt.exists():raise FileExistsError('Use a new receipt; prior failures are retained')
    sys.dont_write_bytecode=True
    os.environ.update(CUDA_VISIBLE_DEVICES='',JAX_PLATFORMS='cpu',PYTHONDONTWRITEBYTECODE='1',
                      OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
    sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
    guard=install_guard();started=time.perf_counter()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=args.pattern)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    receipt={'schema':'bf16-service-local-test-v1','created_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
             'scope':'LOCAL_SYNTHETIC_PROTOCOL_LIFECYCLE_TESTS_NOT_GPU_OR_ROBOT_ACCEPTANCE',
             'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
             'skipped':[str(t)+': '+r for t,r in result.skipped],'success':result.wasSuccessful(),
             'wall_seconds':time.perf_counter()-started,'guard':guard,'python':sys.version,
             'model_calls':0,'hardware_calls':0,'remote_connected':False,
             'source_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ('service','pc','scripts','tests') for p in (ROOT/folder).rglob('*')
                if p.is_file() and p.suffix in ('.py','.ps1')}}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    with args.receipt.open('x',encoding='utf8') as f:json.dump(receipt,f,ensure_ascii=False,indent=2)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__=='__main__':main()
