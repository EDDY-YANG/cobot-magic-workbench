"""Bounded-memory audit for the independent BF16 runner's 1800s window.

Preserves the original event checks and anchored USB identity checks. Only the
finished private process substitutes this streaming reader; production sources,
FP32 audit and on-disk evidence are unchanged.
"""
import hashlib,os
from contextlib import contextmanager
from pathlib import Path

def audit_events(store,receipt,events,*,acceptance=False):
    from agilex_control import h50_acceptance as a
    from agilex_control.h50_async import prepare_output_command,OUTPUT_COMMAND_POLICY
    require=a.require
    expected={r.get('observation_index'):r.get('status') for r in receipt.get('attempts',[]) if r.get('observation_index') is not None}
    decisions={};counts=dict(output_command_checks=0,diagnostic_output_checks=0,gripper_upper_clamps=0,
        gripper_lower_clamps=0,joint_upper_clamps=0,joint_lower_clamps=0)
    check_output=acceptance or receipt.get('output_command_policy') is not None
    if check_output:
        require(receipt.get('output_command_policy')==OUTPUT_COMMAND_POLICY,'shared final output converter evidence missing')
        bounds=receipt.get('output_command_bounds_deg_m');require(isinstance(bounds,list) and len(bounds)==7,'output command bounds missing')
    def output(row,key,kind):
        value=prepare_output_command(row[key],bounds)
        conversion='diagnostic_output_conversion' if kind=='diagnostic_output_checks' else 'output_conversion'
        require(a.canonical(row.get(conversion))==a.canonical(value),'final sampled output conversion differs')
        counts[kind]+=1
        for name in ('upper','lower'):
            counts['gripper_'+name+'_clamps']+=int(value['gripper_'+name+'_clamped'])
            counts['joint_'+name+'_clamps']+=sum(p['clamp_side']==name for p in value['joint_projection'])
    digest=hashlib.sha256();body=0;observer=holds=0;footer=None;header=False;size=0
    store.verify()
    with os.fdopen(store._open(events['path']),'rb') as handle:
        before=os.fstat(handle.fileno());os.fsync(handle.fileno())
        require(before.st_size<=8*1024**3,'independent 30min event artifact exceeds 8GiB budget')
        while True:
            line=handle.readline(2*1024*1024+1)
            if not line:break
            require(len(line)<=2*1024*1024,'event line exceeds 2MiB bound')
            size+=len(line);digest.update(line)
            if not line.strip():continue
            value=a.loads_unique_json(line)
            if not header:
                require(value.get('schema')=='tiger-host-can-tx-evidence-v1','async header missing');header=True;continue
            require(footer is None,'records appear after final footer')
            if value.get('schema')=='tiger-host-can-tx-evidence-summary-v1':footer=value;continue
            body+=1;require(value.get('sequence')==body,'async evidence sequence incomplete')
            row=value.get('record');require(isinstance(row,dict),'actual async envelope missing')
            if row.get('kind')=='RTC_QUEUE_DECISION':
                index=row.get('request_index');require(index in expected and index not in decisions,'duplicate/unexpected queue decision')
                require(row.get('status')==expected[index],'queue decision differs');decisions[index]=row['status']
            if row.get('kind') not in ('NO_SEND_UNSENT_HOLD_AND_ACTUAL','ACCEPTED_REFERENCE_AND_ACTUAL'):continue
            observer+=1
            if row['kind']=='NO_SEND_UNSENT_HOLD_AND_ACTUAL':
                holds+=1
                if acceptance:require(row.get('physical_dispatch_performed') is False and
                    row.get('reference_q_deg_m')==receipt.get('initial_actual_q_deg_m') and row.get('reference_v_deg_m_s')==[0.]*7,
                    'NO_SEND physical reference differs')
            if check_output:
                output(row,'reference_q_deg_m','output_command_checks')
                if row['kind']=='NO_SEND_UNSENT_HOLD_AND_ACTUAL' and row.get('diagnostic_reference_phase')!='ABORT':
                    output(row,'diagnostic_reference_q_deg_m','diagnostic_output_checks')
        after=os.fstat(handle.fileno())
    require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
        (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) and size==before.st_size,'source changed during stream read')
    store.verify()
    require(header and footer is not None and body==events['written'],'async final footer/count incomplete')
    require(set(decisions)==set(expected) and (not acceptance or len(decisions)==103),'queue decisions incomplete')
    require(observer>0 and (not acceptance or holds>0),'actual/reference observer missing')
    if check_output:require(counts['output_command_checks']>0 and (not acceptance or counts['diagnostic_output_checks']==counts['output_command_checks']),
                            'output preflight did not cover ticks')
    return {'path':events['path'],'sha256':digest.hexdigest(),'records':body,'queue_decisions':len(decisions),
        'fsync_after_writer_closed':True,**counts}

@contextmanager
def audit_scope():
    from agilex_control import h50_acceptance as a
    original_anchor,original_audit=a.USBAnchor,a.audit_events
    class LongRunAnchor(original_anchor):
        def read(self,path,*,limit=a.MAX_JSON,flush=False):
            # Only the aggregate receipt grows with the number of calls. Each
            # input/result and manifest retains its existing 16MiB limit.
            if self.relative(path).as_posix()=='evidence/h50-async-run.json' and limit==a.MAX_JSON:limit=512*1024*1024
            return super().read(path,limit=limit,flush=flush)
    try:
        a.USBAnchor=LongRunAnchor;a.audit_events=audit_events
        yield
    finally:a.USBAnchor=original_anchor;a.audit_events=original_audit

def finalize_run_artifacts(cfg,session,step,receipt):
    from agilex_control import h50_acceptance as a
    with audit_scope():return a.finalize_run_artifacts(cfg,session,step,receipt)
