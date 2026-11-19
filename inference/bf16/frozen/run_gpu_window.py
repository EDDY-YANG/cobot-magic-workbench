"""Explicitly approved temporary GPU window, with mandatory FP32 restoration.

This controls only the existing identity-checked model supervisor and independent
offline processes. It contains no robot/camera/CAN command or web-deploy action.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
JOB=Path('/opt/cobot-workbench/lab-packages/2026-10-19-18-12-H50-Model-Supervisor')
OLD=JOB.with_name('2026-10-19-15-38-Pi05-FP32-Deployment')
PYTHON='/home/robot/openpi-workspace/pi05-215abfb217db/env/bin/python'


def write_json(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.writing-'+str(os.getpid()))
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    os.replace(temporary,path)


def event(status,**values):
    print(json.dumps({'at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'status':status,**values}),flush=True)


def get(endpoint):
    return json.load(urllib.request.urlopen('http://127.0.0.1:18882/'+endpoint,timeout=4))


def protected_files():
    return sorted(set(list((JOB/'scripts').glob('*.py'))+list((OLD/'scripts').glob('*.py'))+
                      [p for p in (JOB/'source').rglob('*') if p.is_file() and p.suffix!='.pyc']))


def hashes(paths):
    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def identity_alive(identity):
    path=Path('/proc')/str(identity['pid'])
    if not path.exists():return False
    try:return path.joinpath('stat').read_text().rsplit(') ',1)[1].split()[19]==identity['start_ticks']
    except FileNotFoundError:return False


def pause_model():
    status=get('status')
    if status['status']!='READY' or status['selected_step']!=5000 or status['busy'] or status['lease']['active']:
        raise RuntimeError('FP32 not independently verified idle at selected5k; preserve service')
    instance=json.loads((JOB/'receipts/manager-instance.json').read_text())
    result=subprocess.run([PYTHON,'-I','-B',str(JOB/'scripts/supervisor_control.py'),'stop'],
                          capture_output=True,text=True,timeout=15)
    if result.returncode:raise RuntimeError('Identity-checked stop rejected: '+result.stderr)
    receipt={'status':status,'manager_instance':instance,'stop_stdout':result.stdout,
             'user_authorization':'Tiger permitted temporary model pause provided FP32 source/configuration/deployment route is preserved and restored; no hardware operation.'}
    write_json(ROOT/'receipts/production-before-pause.json',receipt)
    event('APPROVED_FP32_MODEL_PAUSING',manager_pid=instance['pid'],worker_pid=status['worker']['pid'])
    limit=time.monotonic()+40
    while time.monotonic()<limit:
        if not identity_alive(instance) and not identity_alive(status['worker']):return receipt
        time.sleep(.25)
    raise RuntimeError('Known model processes did not fully exit; no offline model started')


def restore_model(before_hashes,paths):
    if hashes(paths)!=before_hashes:raise RuntimeError('Production source changed during window; preserve unknown edits and report')
    try:
        status=get('status')
    except Exception:status=None
    if status is not None:
        if status.get('status')=='READY' and status.get('selected_step')==5000 and not status.get('busy') and not status.get('lease',{}).get('active'):
            event('FP32_ALREADY_READY_NO_RESTART');return {'status':status,'health':get('health')}
        raise RuntimeError('Unexpected live manager preserved; cannot launch duplicate')
    launched=subprocess.run([PYTHON,'-I','-B',str(JOB/'scripts/launch_supervisor.py'),'--step','5000'],
                            capture_output=True,text=True,timeout=20)
    if launched.returncode:raise RuntimeError('Original FP32 launch failed: '+launched.stderr)
    event('RESTORING_ORIGINAL_FP32',launch=launched.stdout.strip())
    deadline=time.monotonic()+900;last=None;last_report=0
    while time.monotonic()<deadline:
        try:
            status=get('status');state=status.get('status')
            if state!=last or time.monotonic()-last_report>30:
                event('FP32_RESTORE_PROGRESS',manager_status=state);last=state;last_report=time.monotonic()
            if state=='FAILED':raise RuntimeError('Original model restoration failed: '+str(status.get('error')))
            if state=='READY':
                health=get('health')
                if (status['selected_step']!=5000 or status['busy'] or status['lease']['active']
                    or not health['ready'] or health['policy']['precision']!='float32'
                    or health['policy']['checkpoint_step']!=5000):
                    raise RuntimeError('Restored FP32 health/step mismatch')
                if hashes(paths)!=before_hashes:raise RuntimeError('FP32 source hash drift')
                return {'status':status,'health':health,'launch':launched.stdout.strip(),
                        'production_source_hashes_unchanged':True}
        except (urllib.error.URLError,TimeoutError,ConnectionError):pass
        time.sleep(2)
    raise TimeoutError('FP32 did not return READY within recovery budget')


def run():
    if socket.gethostname()!='user-MS-7E06':raise RuntimeError('Wrong host')
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(ROOT),'-o','TARGET,UUID'],text=True))['filesystems'][0]
    if mount!={'target':'/data/4090-1tb','uuid':'e76043b6-4ab6-42a5-8b06-4963ee92a567'}:raise RuntimeError('Wrong disk')
    paths=protected_files();before=hashes(paths)
    backup=ROOT/'production-source-backup';backup.mkdir(exist_ok=True)
    for path in paths:
        relative=path.relative_to(Path('/opt/cobot-workbench/lab-packages'))
        target=backup/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and target.read_bytes()!=path.read_bytes():raise RuntimeError('Existing protected backup differs')
        if not target.exists():shutil.copy2(path,target)
    write_json(ROOT/'receipts/production-source-before.json',before)
    state={'status':'STARTING_APPROVED_OFFLINE_WINDOW','pid':os.getpid(),'models':[],
           'production_sources_unchanged':True,'robot_commands':0}
    write_json(ROOT/'receipts/window-status.json',state)
    paused=False
    try:
        # Mark before stop, so a partial stop also enters mandatory recovery.
        paused=True
        pause= pause_model()
        for mode in ['fp32','bf16']:
            event('OFFLINE_MODE_STARTING',mode=mode)
            log=ROOT/'receipts'/('offline-'+mode+'.log')
            started=time.monotonic()
            with log.open('xb',buffering=0) as stream:
                completed=subprocess.run([PYTHON,'-B',str(ROOT/'scripts/offline_runner.py'),'--mode',mode],
                    stdout=stream,stderr=subprocess.STDOUT,cwd=ROOT,timeout=900,
                    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            record={'mode':mode,'returncode':completed.returncode,'seconds':time.monotonic()-started,'log':str(log)}
            state['models'].append(record);write_json(ROOT/'receipts/window-status.json',state)
            event('OFFLINE_MODE_FINISHED',**record)
            if completed.returncode:raise RuntimeError('Offline '+mode+' failed; original logs preserved')
        from compare_results import compare_results,render_markdown
        comparison=compare_results(ROOT/'outputs/fp32/results.json',ROOT/'outputs/bf16/results.json')
        write_json(ROOT/'receipts/comparison.json',comparison)
        (ROOT/'receipts/comparison.md').write_text(render_markdown(comparison),encoding='utf-8')
        state['status']='OFFLINE_COMPARISON_COMPLETE_RESTORING_FP32'
    except BaseException as exc:
        state.update(status='OFFLINE_WINDOW_FAILED_RESTORING_FP32',error=repr(exc),traceback=traceback.format_exc())
        event(state['status'],error=repr(exc))
    finally:
        if paused:
            try:
                recovery=restore_model(before,paths)
                write_json(ROOT/'receipts/production-restored.json',recovery)
                state['FP32_restored']=True;state['production_sources_unchanged']=hashes(paths)==before
                event('ORIGINAL_FP32_READY_RESTORED',step=5000,production_sources_unchanged=state['production_sources_unchanged'])
            except BaseException as exc:
                state.update(FP32_restored=False,recovery_error=repr(exc),recovery_traceback=traceback.format_exc())
                event('FP32_RESTORE_REQUIRES_ATTENTION',error=repr(exc))
        write_json(ROOT/'receipts/window-status.json',state)
    return state


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute-approved-window',action='store_true',required=True)
    parser.parse_args()
    def interrupted(signum,frame):
        raise SystemExit('GPU window interrupted by signal '+str(signum))
    signal.signal(signal.SIGTERM,interrupted)
    event('WINDOW_FINAL',result=run())
