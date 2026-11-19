"""Guarded BF16 backend. Import never imports mixed_policy/JAX or loads a model."""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import gc
import importlib.util
import json
import os
from pathlib import Path,PurePosixPath
import platform
import re
import socket
import subprocess
import sys
import uuid

from . import contract as c

EXPECTED_HOST='user-MS-7E06'
EXPECTED_MOUNT='/data/4090-1tb'
EXPECTED_UUID='e76043b6-4ab6-42a5-8b06-4963ee92a567'
EXPECTED_PYTHON='/opt/cobot-workbench/lab-packages/2026-10-21-19-38-BF16-Smoothing-Live/environment/bin/python'
GPU_LOCK_RELATIVE=(
    '2026-10-19-15-38-Pi05-FP32-Deployment/FP32_GPU_OWNER.lock',
    '2026-10-19-18-12-H50-Model-Supervisor/FP32_GPU_OWNER.lock')
_GPU_SCOPE_CAPABILITY=object()


def verify_dependencies(source,dependencies):
    source=Path(source).resolve(strict=True)
    for relative,sha in dependencies.items():
        path=source/relative;actual=path.resolve(strict=True);actual.relative_to(source)
        if path.is_symlink() or c.file_sha(actual)!=sha:raise RuntimeError('Frozen BF16 dependency differs: '+relative)
    return len(dependencies)


def independent_audit_path(run_dir,path):
    run=Path(run_dir).resolve();resolved=Path(path).resolve();resolved.relative_to(run)
    if resolved!=run/'audit':raise ValueError('Only this new run audit directory is allowed')
    return resolved


def read_config(path):
    raw=Path(path).read_bytes();cfg=c.strict_json(raw)
    if (cfg.get('schema')!=c.CONFIG_SCHEMA or cfg.get('bind')!=c.HOST or cfg.get('port')!=c.PORT
        or cfg.get('host')!=EXPECTED_HOST or cfg.get('mount')!={'target':EXPECTED_MOUNT,'uuid':EXPECTED_UUID}
        or cfg.get('python')!=EXPECTED_PYTHON or cfg.get('lease_ttl_s')!=c.LEASE_TTL
        or cfg.get('allowed_steps')!=list(c.ALLOWED_STEPS)):
        raise ValueError('Fixed service host/mount/interpreter/port/RTC lease contract differs')
    for key in ('remote_root','bf16_source_root','historical_dataset_root'):
        value=cfg.get(key)
        if not isinstance(value,str) or not PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts:
            raise ValueError('Explicit absolute service/dependency/dataset paths required')
    if cfg['remote_root']==cfg['bf16_source_root']:raise ValueError('Independent service must not reuse old experiment root')
    deps=cfg.get('dependencies_sha256')
    required={'scripts/mixed_policy.py','scripts/offline_guard.py','scripts/offline_dataset.py',
              'frozen_source/fp32_http.py','frozen_source/frozen_rtc_core.py','frozen_source/gpu_inventory.py','source/uv.lock'}
    if not isinstance(deps,dict) or not required<=set(deps):raise ValueError('Frozen BF16 dependency manifest is incomplete')
    for name,digest in deps.items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or re.fullmatch('[0-9a-f]{64}',str(digest)) is None:
            raise ValueError('Invalid relative dependency SHA identity')
    return cfg,c.file_sha(path)


def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module


def verify_online_environment(cfg,run_dir,step,execute_online):
    """All checks happen before old mixed_policy import or GPU construction."""
    c.step_value(step)
    if execute_online is not True:raise RuntimeError('Explicit --execute-online required; no backend constructed')
    if platform.system()!='Linux' or socket.gethostname()!=EXPECTED_HOST:raise RuntimeError('Real BF16 backend requires the fixed Linux4090 host')
    if os.path.abspath(sys.executable)!=EXPECTED_PYTHON or os.path.abspath(sys.prefix)!=str(Path(EXPECTED_PYTHON).parents[1]):
        raise RuntimeError('Exact frozen BF16 interpreter/environment required')
    root=Path(cfg['remote_root']).resolve(strict=True)
    if Path(__file__).resolve().parents[1]!=root:raise RuntimeError('Service entrypoint is outside its independent root')
    run=Path(run_dir).resolve(strict=True);run.relative_to(root/'runs')
    if run==root/'runs' or run.is_symlink():raise RuntimeError('A dedicated new run directory is required')
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(run),'-o','TARGET,UUID'],text=True,timeout=10))['filesystems']
    if len(mount)!=1 or mount[0]!=cfg['mount']:raise RuntimeError('Actual service SSD mount/UUID differs')
    source=Path(cfg['bf16_source_root']).resolve(strict=True)
    verify_dependencies(source,cfg['dependencies_sha256'])
    return {'host':EXPECTED_HOST,'mount':mount[0],'python':sys.executable,'dependencies_verified':len(cfg['dependencies_sha256']),
            'old_sources_written':False,'new_run_dir':str(run)}


class RealBackend:
    """Construct only inside online_backend's held two-lock GPU scope."""
    synthetic_fixture=False
    def __init__(self,cfg,run_dir,step,*,_capability=None):
        if _capability is not _GPU_SCOPE_CAPABILITY:raise RuntimeError('Real backend construction requires the held GPU scope capability')
        self.cfg,self.run_dir,self.step=cfg,Path(run_dir),step
        self.policy=None;self.construction_started=False;self.construction_completed=False;self.stopped=False
        self.warmup={};self.identity={};self.metadata={};self.rtc={};self.audit={}

    def load(self):
        source=Path(self.cfg['bf16_source_root']);tag=uuid.uuid4().hex
        mixed=load_module('_independent_bf16_mixed_'+tag,source/'scripts/mixed_policy.py')
        dataset=load_module('_independent_bf16_dataset_'+tag,source/'scripts/offline_dataset.py')
        manifest,manifest_sha,samples=dataset.load_dataset(Path(self.cfg['historical_dataset_root']))
        if not samples:raise RuntimeError('Verified actual historical warmup inputs required')
        bootstrap=next((value for value in samples if value[1]['rtc']['enabled'] is False),None)
        continuation=next((value for value in samples if value[1]['rtc']['enabled'] is True),None)
        if bootstrap is None or continuation is None:raise RuntimeError('Historical bootstrap and RTC warmup inputs required')
        self.construction_started=True
        self.policy=mixed.create_mixed_adapter(step=self.step,num_steps=10,seed=42)
        self.construction_completed=True
        audit_dir=self.run_dir/'audit'
        original=mixed._receipt_path
        def independent_receipt_path(path):
            return independent_audit_path(self.run_dir,path)
        try:
            # Unique imported in-memory module only. No old file/global owner is
            # edited; old ROOT/source/loader identity checks remain untouched.
            mixed._receipt_path=independent_receipt_path
            self.audit=self.policy.audit_and_compile(bootstrap[1],audit_dir)
        finally:mixed._receipt_path=original
        validators=c.load_validators()
        for _,observation,_ in (bootstrap,continuation,continuation):
            result=self.infer_rtc(observation);validators.encode_result(result,0.)
        self.metadata=deepcopy(self.policy.metadata);self.rtc=deepcopy(self.policy.rtc)
        self.warmup={'completed':True,'calls':3,'real_historical_inputs':True,'dataset_manifest_sha256':manifest_sha,
            'source_checkpoint_step':manifest['model']['checkpoint_step'],'loaded_checkpoint_step':self.step,
            'bootstrap_and_continuation':True,'numerical_quality_accepted':False,
            'other_step_use':'STRUCTURE_AND_RUNTIME_ONLY_NOT_NUMERICAL_ADMISSION' if self.step!=5000 else 'HISTORICAL_WARMUP_NOT_QUALITY_ADMISSION'}
        self.identity={'original_bf16_implementation_sha256':c.file_sha(source/'scripts/mixed_policy.py'),
            'mixed_precision_audit_sha256':c.canonical_sha(self.audit),'dependencies_manifest_sha256':c.canonical_sha(self.cfg['dependencies_sha256']),
            'service_sources_sha256':{name:c.file_sha(Path(__file__).with_name(name)) for name in ('contract.py','backend.py','server.py')},
            'normalization_sha256':self.metadata['normalization_sha256'],'tokenizer_sha256':self.metadata['tokenizer_sha256'],
            'source_commit':self.metadata['source_commit']}

    def infer_rtc(self,observation):
        # Preserve original MixedPolicy result, including static capacity35 in
        # rtc.previous_valid_steps during native requests. Contract validation
        # separately binds enabled/trial/request/seed and actual prefix hash.
        return self.policy.infer_rtc(observation,return_raw=False)

    def close(self):
        if self.policy is not None:
            # Wait parameter creation/casts too; inference raw outputs already
            # block_until_ready in the frozen policy before returning.
            for leaf in self.policy.jax.tree_util.tree_leaves(self.policy.state):
                block=getattr(leaf,'block_until_ready',None)
                if callable(block):block()
            self.policy=None;gc.collect()
        self.stopped=not self.construction_started or self.construction_completed
        return self.stopped


@contextmanager
def online_backend(cfg,run_dir,step,*,execute_online=False,stop_event=None,lifecycle=None):
    lifecycle={} if lifecycle is None else lifecycle
    lifecycle.update(locks_released=True,worker_stopped=True)
    checks=verify_online_environment(cfg,run_dir,step,execute_online)
    source=Path(cfg['bf16_source_root'])
    guard=load_module('_independent_bf16_guard_'+uuid.uuid4().hex,source/'scripts/offline_guard.py')
    expected=tuple(str(Path(EXPECTED_MOUNT)/'RobotData/03_Files_and_Code'/name) for name in GPU_LOCK_RELATIVE)
    if tuple(map(str,guard.LOCK_PATHS))!=expected:raise RuntimeError('Existing two GPU owner locks changed')
    scope=guard.acquire_gpu_scope(inventory_source=source/'frozen_source/gpu_inventory.py',
        expected_inventory_sha256=cfg['dependencies_sha256']['frozen_source/gpu_inventory.py'])
    try:
        with scope as receipt:
            if receipt.get('locks_held') is not True:raise RuntimeError('Both existing GPU locks must be held')
            lifecycle['locks_released']=False
            backend=RealBackend(cfg,run_dir,step,_capability=_GPU_SCOPE_CAPABILITY)
            try:
                if stop_event is not None and stop_event.is_set():raise RuntimeError('Shutdown requested before model construction')
                lifecycle['worker_stopped']=False
                backend.load();backend.guard_receipt=receipt;backend.environment_checks=checks
                if stop_event is not None and stop_event.is_set():raise RuntimeError('Shutdown requested while loading; no serving')
                yield backend
            finally:lifecycle['worker_stopped']=backend.close()
    finally:lifecycle['locks_released']=True
