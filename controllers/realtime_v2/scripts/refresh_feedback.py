"""Refresh existing Hub feedback only. No CAN socket/query/reset or inference."""
from pathlib import Path
import argparse,json,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
from src.live_io import ExistingHub,require
from src.runtime_guard import can_counters,verify_zero_tx

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute-readonly',action='store_true');p.add_argument('--config',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    require(a.execute_readonly and sys.platform=='linux','Explicit Linux read-only operation required')
    cfg=json.loads(a.config.read_text());sys.path.insert(0,cfg['remote_bundle'])
    from agilex_control import storage
    from agilex_control.h50_journal import H50USBJournal
    storage.verify_usb(cfg)
    require(a.output.is_relative_to(ROOT) and not a.output.exists(),'New private USB evidence required')
    hub=ExistingHub(cfg,full_status=True);before=can_counters();rows=[]
    for _ in range(40):
        rows.append(hub.feedback());time.sleep(.02)
    zero=verify_zero_tx(before,can_counters())
    progression={k:rows[-1]['source_ns_by_component'][k]-rows[0]['source_ns_by_component'][k]
                 for k in ('q12','q34','q56','gripper')}
    require(all(v>0 for v in progression.values()),'Actual CAN source time did not advance')
    report={'status':'EXISTING_HUB_CAN_FEEDBACK_REFRESHED_NO_TX','source_progression_ns':progression,
        'source_time_rewritten':False,'producer':hub.producer,'storage_binding':hub.storage_binding,
        'bus_reset':False,'new_device_readers':0,'model_calls':0,'zero_tx':zero,'samples':rows,
        'firmware_version_queried':False,'maximum_speed_raw_unit':'Unverified firmware-dependent unit; do not infer0.3rad/s from raw300'}
    journal=H50USBJournal(cfg,ROOT)
    try:journal.write_json(a.output,report)
    finally:journal.close()
    f=rows[-1]['feedback']
    print(json.dumps({'status':report['status'],'samples':len(rows),'zero_tx':zero,
        'source_progression_ms':{k:v/1e6 for k,v in progression.items()},
        'latest':{k:f.get(k) for k in ('q_deg','grip_m','age_joint_s','age_status_s','age_motor_s','age_gripper_s','feedback_fresh','observer_error')},
        'output':str(a.output)},ensure_ascii=False))

if __name__=='__main__':main()
