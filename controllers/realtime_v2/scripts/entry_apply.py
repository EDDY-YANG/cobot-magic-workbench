"""Default inspect never connects. RUN requires a new explicit human order."""
import argparse,base64,json,os,sys,socket,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
from src.window_contract import validate_window_seconds

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',nargs='?',choices=('inspect','run','stop'),default='inspect')
    p.add_argument('--binding',type=Path);p.add_argument('--binding-sha256')
    p.add_argument('--duration',type=float,default=40.)
    prompts=p.add_mutually_exclusive_group()
    prompts.add_argument('--prompt',default='右臂将红色方块放入绿色桶')
    prompts.add_argument('--prompt-base64',help='UTF-8 prompt as strict Base64 for the PC entry')
    p.add_argument('--trial-index',type=int,default=0)
    for flag in ('authorize-motion','leader-isolated','onsite-confirmed','current-start-confirmed','stop-on-stdin-eof'):
        p.add_argument('--'+flag,action='store_true')
    p.add_argument('--runtime-child',action='store_true',help=argparse.SUPPRESS)
    a=p.parse_args()
    if a.prompt_base64 is not None:
        try:
            if len(a.prompt_base64)>4096:raise ValueError('encoded prompt exceeds bound')
            a.prompt=base64.b64decode(a.prompt_base64,validate=True).decode('utf-8')
        except (ValueError,UnicodeDecodeError) as exc:
            p.error('Strict Base64 UTF-8 prompt required: '+type(exc).__name__)
    if not a.prompt.strip() or len(a.prompt)>200 or any(c in a.prompt for c in ('\n','\r','\x00')):
        p.error('Single-line task prompt required')
    if a.action=='inspect':
        print(json.dumps({'status':'PREPARATION_ONLY_WAITING_FOR_NEW_HUMAN_RUN_ORDER',
            'policy_window_s':40,'window_min_s':1,'window_max_s':1800,'motion_started':False,'model_calls':0,
            'automatic_execution':False,'physical_stop_tested':False,'prompt':a.prompt},ensure_ascii=True));return 0
    if not a.binding or not a.binding_sha256:p.error('Exact prepared binding and SHA required')
    if a.action=='run' and not all((a.authorize_motion,a.leader_isolated,a.onsite_confirmed,a.current_start_confirmed)):
        p.error('New explicit motion order and current onsite/start/isolation confirmations required')
    if a.action=='run':
        try:a.duration=validate_window_seconds(a.duration)
        except (ValueError,TypeError) as exc:p.error(str(exc))
        if not 0<=a.trial_index<=1_000_000:p.error('Bounded trial index required')
    if sys.platform!='linux':p.error('Physical entry only runs on the staged robot host')
    from src.runtime_guard import sha
    if sha(a.binding)!=a.binding_sha256:p.error('Prepared binding SHA differs')
    b=json.loads(a.binding.read_text())
    if a.action=='stop':
        # An original-source update must never disable this independent stop.
        if socket.gethostname()!=b['host'] or Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=b['boot_id']:
            p.error('Stop host/boot differs')
        mount=json.loads(subprocess.check_output(['findmnt','-J','-T',b['usb_mount'],
            '-o','TARGET,UUID,OPTIONS,MAJ:MIN'],text=True))['filesystems'][0]
        if mount['target']!=b['usb_mount'] or mount['uuid']!=b['usb_uuid'] or mount['maj:min']!=b['major_minor'] or 'rw' not in mount['options'].split(','):
            p.error('Stop USB identity differs')
        if str(ROOT)!=b['runtime_root'] or ROOT.is_symlink() or os.stat(ROOT).st_dev!=os.stat(b['usb_mount']).st_dev:
            p.error('Stop runtime root differs')
        directory=os.open(ROOT,os.O_DIRECTORY|os.O_RDONLY|os.O_NOFOLLOW)
        try:
            fd=os.open('STOP-APPLY',os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(fd,'w') as handle:
                handle.write('{"stop_new_targets":true,"hardware_estop":false}\n');handle.flush();os.fsync(handle.fileno())
        finally:os.close(directory)
        print(json.dumps({'status':'STOP_NEW_V2_TARGETS_REQUESTED','last_target_may_finish':True}));return 0
    if not a.runtime_child:
        python=Path(b['python']).absolute();acados=Path(b['acados_source']).absolute()
        if ROOT not in python.parents or ROOT not in acados.parents:p.error('Private environment/acados required')
        env=dict(os.environ,ACADOS_SOURCE_DIR=str(acados),LD_LIBRARY_PATH=str(acados/'lib'),
                 PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
        env.pop('PYTHONPATH',None)
        os.execve(str(python),[str(python),'-I','-B',str(Path(__file__).resolve()),*sys.argv[1:],'--runtime-child'],env)
    from src.runtime_guard import load_binding
    binding,cfg,storage=load_binding(a.binding,a.binding_sha256,ROOT,physical=True)
    if a.duration!=binding.get('policy_window_s'):p.error('Requested window differs from prepared binding')
    from src.runtime_apply import execute_physical
    execute_physical(binding,cfg,storage,a);return 0

if __name__=='__main__':raise SystemExit(main())
