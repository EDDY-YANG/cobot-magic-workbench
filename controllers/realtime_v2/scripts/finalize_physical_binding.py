"""Bind completed read-only preflight/build to the explicit physical entry.
This creates no motion authorization, model request, or CAN socket.
"""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
from src.runtime_guard import load_binding,sha
from src.live_io import require
from src.window_contract import validate_window_seconds

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binding',type=Path,required=True);p.add_argument('--binding-sha256',required=True)
    p.add_argument('--preflight',type=Path,required=True);p.add_argument('--build-receipt',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--duration',type=float,default=40.);a=p.parse_args()
    duration=validate_window_seconds(a.duration)
    b,cfg,storage=load_binding(a.binding,a.binding_sha256,ROOT)
    for f in (a.preflight,a.build_receipt):require(f.resolve().is_relative_to(ROOT),'Private evidence path required')
    f=json.loads(a.preflight.read_text());build=json.loads(a.build_receipt.read_text())
    require(f['status']=='READ_ONLY_CONTROLLER_PREFLIGHT_COMPLETED' and f['motion_groups_sent']==0,
            'Actual read-only limits preflight required')
    require(f['source_binding_sha256']==a.binding_sha256,'Preflight/source binding differs')
    require(build['status']=='REAL_ACADOS_BUILD_AND_SYNTHETIC_WARMUP_PASSED','Real solver prebuild/warmup required')
    require(not a.output.exists() and a.output.parent.is_dir() and a.output.is_relative_to(ROOT),'New private output required')
    b.update(schema='tiger-isolated-v2-physical-prepared-v1',APPLY_supported=True,
        physical_motion_validated=False,motion_authorized_by_binding=False,policy_window_s=duration,
        supported_window_s={'min':1.,'max':1800.},
        bounds_rad_m=f['bounds_rad_m'],bounds_source='ACTUAL_CONTROLLER_QUERY_INTERSECT_SDK_DOCUMENTED',
        controller_preflight=str(a.preflight),
        controller_preflight_sha256=sha(a.preflight),solver_build_receipt=str(a.build_receipt),
        solver_build_receipt_sha256=sha(a.build_receipt),
        execution_requires='New explicit operator RUN with current onsite/isolation/start confirmations')
    from agilex_control.h50_journal import H50USBJournal
    journal=H50USBJournal(cfg,ROOT)
    try:journal.write_json(a.output,b)
    finally:journal.close()
    print(json.dumps({'prepared_binding':str(a.output),'sha256':sha(a.output),'motion_started':False}))

if __name__=='__main__':main()
