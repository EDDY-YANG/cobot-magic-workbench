"""Web lifecycle adapter for this private V2 package; never selects a model.

inspect is read-only. Acceptance is real30s NO_SEND. Only explicit run calls
the existing physical entry; cancellation propagates inherited stdin EOF.
"""
from pathlib import Path
import argparse
import base64
from datetime import datetime
import json
import os
import socket
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from src.runtime_guard import require, sha, source_files, load_binding, verify_model
from src.live_io import PinnedLoadedModel, public_receipt
from src.v2_tracking_guard import tracking_policy
from src.window_contract import validate_window_seconds, timing_policy
from src.joint_boundary_repair import policy as joint_reference_repair_policy
from src.deployment_index import index_paths, write_deployment_index
from src.manual_speed_control import SCHEMA as SPEED_SCHEMA, LIMITS as SPEED_LIMITS
from src.speed_teaching import collection_contract
from src.bridge_step_policy import SCHEMA as STEP_WARNING_SCHEMA,policy as bridge_step_policy


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def emit(value):
    print(json.dumps(public_receipt(value), ensure_ascii=False), flush=True)


def private(path):
    p = Path(path)
    require(p.is_absolute() and p.resolve().is_relative_to(ROOT.resolve()) and not p.is_symlink(),
            'Receipt path must stay in this private runner')
    return p


def environment():
    profile = read(ROOT/'web-profile.json')
    require(profile['runtime_root'] == str(ROOT), 'Wrong V2 runtime root')
    require(socket.gethostname() == profile['host'], 'Wrong V2 host')
    require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() == profile['boot_id'], 'V2 host rebooted')
    require(source_files(ROOT) == profile['runtime_sources'], 'V2 installed source changed')
    canonical = Path(profile['canonical_root'])
    sys.path[:0] = [str(canonical), profile['schema_root']]
    from agilex_control import storage
    require(Path(storage.__file__).resolve() == canonical/'agilex_control/storage.py', 'Foreign storage module')
    cfg = read(profile['config_path'])
    storage.verify_usb(cfg)
    require(str(cfg['usb_uuid']).upper() == profile['usb_uuid'].upper(), 'USB UUID changed')
    require(ROOT.is_relative_to(Path(cfg['usb'])), 'Private runner is not on selected USB')
    dev = os.stat(ROOT).st_dev
    require(f'{os.major(dev)}:{os.minor(dev)}' == profile['major_minor'], 'USB device changed')
    return profile, cfg, storage


def save(cfg, path, value):
    from agilex_control.h50_journal import H50USBJournal
    with_journal = H50USBJournal(cfg, ROOT)
    try:
        with_journal.write_json(private(path), public_receipt(value))
    finally:
        with_journal.close()


def binding_current(admission, step=None, task=None):
    b, _, _ = load_binding(private(admission['binding']), admission['binding_sha256'], ROOT)
    current = PinnedLoadedModel('fp32', b['model']['step']).bind()
    verify_model(b, current)
    from src.temporal_rpc import TimeQPHTTP
    require(TimeQPHTTP().request('GET', '/health').get('instance_id') == b['timeqp']['instance_id'],
            'TimeQP instance changed; new V2 NO_SEND required')
    require(current.get('instance') == b['model'].get('instance'), 'Model worker/selection changed')
    require(admission['runtime_profile_sha256'] == sha(ROOT/'web-profile.json'), 'V2 profile changed')
    require(step is None or step == b['model']['step'] == admission['step'], 'V2 checkpoint differs')
    require(task is None or task == admission['task'], 'V2 prompt requires matching NO_SEND')
    operation_path = private(admission['report'])
    require(sha(operation_path) == admission['report_sha256'], 'V2 acceptance evidence changed')
    operation = read(operation_path)
    require(operation.get('status') == 'REAL_IO_NO_SEND_COMPLETED_NOT_MOTION_ADMISSION', 'V2 NO_SEND did not pass')
    require(operation.get('evidence', {}).get('complete') is True, 'V2 streamed evidence incomplete')
    require(operation.get('zero_tx', {}).get('right_TX_delta') == 0 and
            operation.get('zero_tx', {}).get('left_TX_delta') == 0 and
            operation.get('protected_files_unchanged') is True, 'V2 zero-send/source proof missing')
    return b, current


def metadata():
    result = {'ready': False, 'error': None, 'duration_s': 40, 'window_min_s':1, 'window_max_s':1800,
              'acceptance_duration_s': 30,
              'experimental': True, 'acceptance': {'validated': False}, 'latest_physical': None,
              'tracking_policy': tracking_policy(),
              'timing_policy': timing_policy(),
              'joint_reference_repair_policy':joint_reference_repair_policy(),
              'bridge_step_policy':bridge_step_policy(),
              'manual_run_without_nosend_supported': True,
              'manual_speed_collection': collection_contract(),
              'manual_speed_contract': {'schema':SPEED_SCHEMA,'limits':dict(SPEED_LIMITS),
                                        'supported':True,'mode':'TIMEQP_TARGET_NEXT_PACKET'},
              'manual_run_policy': 'EXPLICIT_OPERATOR_REQUEST_FRESH_BINDING_NO_FABRICATED_PASS',
              'window_contract': {'min_duration_s':1,'max_duration_s':1800,
                                  'default_duration_s':40,'acceptance_duration_s':30}}
    try:
        _, cfg, _ = environment()
        result['deployment_indexes'] = index_paths(cfg)
        from src.temporal_rpc import TimeQPHTTP, SCHEMA, OFFICIAL_SHA, implementation_sha256
        health = TimeQPHTTP().request('GET', '/health')
        require(health.get('ready') is True and health.get('schema') == SCHEMA and
                health.get('official_sha256') == OFFICIAL_SHA and
                health.get('implementation_sha256') == implementation_sha256(), 'Private TimeQP version unavailable')
        result['timeqp_implementation'] = health.get('implementation_mode')
        result['ready'] = True
        if (ROOT/'latest-physical.json').exists():
            result['latest_physical'] = read(ROOT/'latest-physical.json')
        if (ROOT/'web-admission.json').exists():
            admission = read(ROOT/'web-admission.json')
            try:
                binding_current(admission)
                result['acceptance'] = {**admission, 'validated': True}
            except Exception as exc:
                result['acceptance'] = {**admission, 'validated': False, 'reason': str(exc)}
    except Exception as exc:
        result['error'] = str(exc)
    return result


def child(script, args, log):
    """Do not consume stdin: the guarded child owns EOF cancellation."""
    command = [str(ROOT/'environment/bin/python'), '-I', '-B', str(ROOT/'scripts'/script), *args]
    last = None
    with private(log).open('x', encoding='utf8') as output:
        p = subprocess.Popen(command, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding='utf8', bufsize=1)
        for line in p.stdout:
            output.write(line)
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                if value.get('schema')==STEP_WARNING_SCHEMA and value.get('event')=='V2_REFERENCE_STEP_WARNING':
                    emit(value)
                    continue
                if value.get('schema')==SPEED_SCHEMA and value.get('event') in ('V2_SPEED_READY','V2_SPEED_STATUS'):
                    emit(value)
                    continue
                last = value
                if value.get('run_dir'):
                    emit({'status': 'V2_WEB_RUNNING', 'run_dir': value['run_dir'],
                          'child_status': value.get('status')})
        code = p.wait()
    return code, last


def invoke_checked(script, args, log):
    code, value = child(script, args, log)
    require(code == 0 and value is not None, 'V2 preparation failed; see '+str(log))
    return value


def acceptance(profile, cfg, args, tag):
    output = ROOT/'receipts'/(tag+'-nosend-binding.json')
    binding = invoke_checked('capture_binding.py', [
        '--execute-readonly', '--canonical-root', profile['canonical_root'],
        '--schema-root', profile['schema_root'], '--config-path', profile['config_path'],
        '--precision', 'fp32', '--step', str(args.step), '--acados-source', str(ROOT/'deps/acados'),
        '--solver-dir', str(ROOT/'build/mpc'), '--output', str(output)],
        ROOT/'receipts'/(tag+'-capture.log'))
    binding_args = ['--binding', str(output), '--binding-sha256', binding['sha256']]
    code, result = child('entry.py', ['continuous', '--execute-online', *binding_args,
        '--duration', '30', '--bootstrap', 'native_first', '--prompt-base64', args.prompt_base64,
        '--stop-on-stdin-eof'], ROOT/'receipts'/(tag+'-nosend.log'))
    require(result is not None and result.get('run_dir'), 'NO_SEND returned no run receipt')
    run = private(result['run_dir']); operation = read(run/'operation.json')
    args._finished_run_dir = str(run); args._child_returncode = code
    if code or operation.get('status') != 'REAL_IO_NO_SEND_COMPLETED_NOT_MOTION_ADMISSION':
        failure = operation.get('error') or operation.get('finalization_error') or operation.get('evidence_error')
        save(cfg, ROOT/'web-admission.json', {'validated': False, 'step': args.step, 'task': args.task,
             'error': failure, 'run_dir': str(run)})
        return {'status': 'FAILED_RETAINED', 'error': failure,
                'run_dir': str(run), 'result': operation}, 1
    current = PinnedLoadedModel('fp32', args.step).bind()
    verify_model(read(output), current)
    value = {'validated': True, 'step': args.step, 'task': args.task, 'duration_s': 30,
        'tracking_policy': tracking_policy(), 'physical_tracking_guard_tested_by_NO_SEND': False,
        'binding': str(output), 'binding_sha256': binding['sha256'],
        'runtime_profile_sha256': sha(ROOT/'web-profile.json'),
        'model_fingerprint': current['health']['contract_fingerprint'],
        'model_instance': current['instance'], 'run_dir': str(run),
        'report': str(run/'operation.json'), 'report_sha256': sha(run/'operation.json')}
    binding_current(value, args.step, args.task)
    save(cfg, ROOT/'web-admission.json', value)
    return {'status': 'V2_WEB_NO_SEND_VALIDATED', 'run_dir': str(run), 'report': value['report'],
            'acceptance': value, 'robot_commands': 0}, 0


def run_window(profile, cfg, args, tag):
    duration = validate_window_seconds(args.duration)
    require(all((args.authorize_motion, args.leader_isolated, args.onsite_confirmed, args.current_start_confirmed)),
            'Explicit current human motion instruction required')
    manual = bool(getattr(args, 'operator_skip_no_motion_acceptance', False))
    if manual:
        fresh = ROOT/'receipts'/(tag+'-manual-identity-binding.json')
        captured = invoke_checked('capture_binding.py', [
            '--execute-readonly', '--operator-skip-no-motion-acceptance',
            '--canonical-root', profile['canonical_root'], '--schema-root', profile['schema_root'],
            '--config-path', profile['config_path'], '--precision', 'fp32', '--step', str(args.step),
            '--acados-source', str(ROOT/'deps/acados'), '--solver-dir', str(ROOT/'build/mpc'),
            '--output', str(fresh)], ROOT/'receipts'/(tag+'-manual-capture.log'))
        b, _, _ = load_binding(fresh, captured['sha256'], ROOT)
        current = PinnedLoadedModel('fp32', args.step).bind()
        verify_model(b, current)
        bindargs = ['--binding', str(fresh), '--binding-sha256', captured['sha256']]
    else:
        admission = read(ROOT/'web-admission.json')
        b, _ = binding_current(admission, args.step, args.task)
        bindargs = ['--binding', admission['binding'], '--binding-sha256', admission['binding_sha256']]
    preflight = ROOT/'receipts'/(tag+'-controller-preflight.json')
    invoke_checked('prepare_hardware.py', [*bindargs, '--execute-readonly', '--output', str(preflight)],
                   ROOT/'receipts'/(tag+'-controller-preflight.log'))
    require(read(preflight)['bounds_rad_m'] == b['bounds_rad_m'], 'Current controller limits differ from prepared source bounds')
    prepared = ROOT/'receipts'/(tag+'-physical-binding.json')
    result = invoke_checked('finalize_physical_binding.py', [*bindargs, '--preflight', str(preflight),
        '--build-receipt', profile['solver_build_receipt'], '--output', str(prepared), '--duration', str(duration)],
        ROOT/'receipts'/(tag+'-prepare.log'))
    save(cfg, ROOT/'active-web-physical-binding.json', {'path': str(prepared), 'sha256': result['sha256']})
    code, output = child('entry_apply.py', ['run', '--binding', str(prepared), '--binding-sha256', result['sha256'],
        '--duration', str(duration), '--prompt-base64', args.prompt_base64, '--authorize-motion', '--leader-isolated',
        '--onsite-confirmed', '--current-start-confirmed', '--stop-on-stdin-eof'], ROOT/'receipts'/(tag+'-apply.log'))
    require(output is not None and output.get('run_dir'), 'Physical runner returned no final receipt')
    run = private(output['run_dir']); operation = read(run/'operation.json')
    args._finished_run_dir = str(run); args._child_returncode = code
    summary_present = (run/'physical-summary.json').is_file()
    summary = read(run/'physical-summary.json') if summary_present else {}
    failure = (summary.get('error') or operation.get('error') or operation.get('finalization_error')
               or '; '.join(operation.get('errors') or operation.get('monitor_errors') or []) or None)
    complete = (code == 0 and operation['status'] == 'PHYSICAL_WINDOW_COMPLETED_AND_POSITION_SETTLED'
                and operation.get('evidence', {}).get('complete') is True)
    attempt = summary.get('last_tick_attempt') or {}
    elapsed = ((attempt['now_ns']-summary['start_ns'])/1e9 if attempt.get('now_ns') else None)
    last = {'status': operation['status'], 'completed_window': complete, 'elapsed_s': elapsed,
            'error': failure, 'run_dir': str(run), 'policy_window_s': duration,
            'physical_summary_present': summary_present,
            'operator_skip_no_motion_acceptance': manual,
            'admission_basis': 'OPERATOR_DIRECT_WITH_FRESH_BINDING' if manual else 'MATCHED_NO_SEND',
            'step': args.step, 'task': args.task, 'physical_task_success': 'REQUIRES_OPERATOR_REVIEW',
            'speed_teaching':operation.get('speed_teaching')}
    save(cfg, ROOT/'latest-physical.json', last)
    return {'status': 'V2_WEB_WINDOW_COMPLETED' if complete else 'FAILED_RETAINED',
            'error': failure, 'run_dir': str(run), 'report': str(run/'operation.json'),
            'result': last}, 0 if complete else 1


def execute_indexed(profile, cfg, args, tag):
    """Index only finalized runs; an index failure never hides the original error."""
    try:
        result, code = (run_window if args.action == 'run' else acceptance)(profile, cfg, args, tag)
    except Exception as exc:
        if not getattr(args, '_finished_run_dir', None):
            raise
        result, code = {'status': 'FAILED_RETAINED', 'error': type(exc).__name__ + ': ' + str(exc),
                        'run_dir': args._finished_run_dir}, 1
    try:
        indexed = write_deployment_index(cfg, args._finished_run_dir, action=args.action,
            requested_window_s=args.duration, step=args.step, prompt=args.task,
            web_status=result['status'], web_error=result.get('error'),
            child_returncode=args._child_returncode)
    except Exception as exc:
        indexed = {'saved': False, 'error': type(exc).__name__ + ': ' + str(exc),
                   'original_data_copied_or_moved': False}
        result['execution_result_status'] = result['status']
        result['status'] = 'FAILED_RETAINED'
        result['error'] = result.get('error') or 'Deployment index save failed: ' + indexed['error']
        code = 1
    result['deployment_index'] = indexed
    return result, code


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('inspect', 'acceptance', 'run', 'stop'))
    p.add_argument('--step', type=int, choices=(2000, 5000, 10000, 15000, 20000))
    p.add_argument('--prompt-base64');p.add_argument('--duration', type=float)
    for name in ('execute-online','authorize-motion','leader-isolated','onsite-confirmed','current-start-confirmed',
                 'operator-skip-no-motion-acceptance','runtime-child'):
        p.add_argument('--'+name, action='store_true')
    args = p.parse_args()
    require(sys.platform == 'linux', 'Real web adapter runs only on robot Linux')
    if not args.runtime_child:
        env = dict(os.environ, ACADOS_SOURCE_DIR=str(ROOT/'deps/acados'), LD_LIBRARY_PATH=str(ROOT/'deps/acados/lib'),
                   OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        py = str(ROOT/'environment/bin/python')
        os.execve(py, [py, '-I', '-B', str(Path(__file__).resolve()), *sys.argv[1:], '--runtime-child'], env)
    if args.action == 'inspect':
        emit(metadata());return 0
    require(args.execute_online, 'Explicit online action required')
    if args.action == 'stop':
        # Preserve independent STOP even if execution sources changed. The
        # original stop entry still checks exact host, boot, USB and directory.
        b = read(private(ROOT/'active-web-physical-binding.json'))
        py = str(ROOT/'environment/bin/python')
        os.execv(py, [py, '-I', '-B', str(ROOT/'scripts/entry_apply.py'), 'stop',
                     '--binding', b['path'], '--binding-sha256', b['sha256']])
    profile, cfg, storage = environment()
    tag = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')+'-web-'+uuid.uuid4().hex[:8]
    storage.require_not_stopped(cfg)
    require(args.step is not None, 'Checkpoint required')
    if args.action == 'run':args.duration=validate_window_seconds(args.duration)
    else:require(args.duration == 30, 'Acceptance remains 30 seconds')
    args.task = base64.b64decode(args.prompt_base64 or '', validate=True).decode('utf8')
    from agilex_control.h50_prompt import validate_prompt
    validate_prompt(args.task)
    require(not any(ord(c)<32 or ord(c)==127 for c in args.task), 'Single line prompt required')
    result, code = execute_indexed(profile, cfg, args, tag)
    emit(result);return code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit({'status':'FAILED_RETAINED','error':type(exc).__name__+': '+str(exc),
              'automatic_retry':False,'automatic_return_to_zero':False})
        raise SystemExit(1)
