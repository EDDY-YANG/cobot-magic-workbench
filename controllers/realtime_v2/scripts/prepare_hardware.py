"""Explicit preparation: existing Hub + six read-only controller-limit queries.
No model inference, enable, mode handoff, target command, zero or disable.
"""
from pathlib import Path
import argparse,json,sys,time,threading
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
from src.runtime_guard import load_binding,can_counters,check_stop
from src.live_io import ExistingHub,owned_session,public_receipt,require
from src.feedback_cache import FeedbackCache
from src.physical_window import BoundedTxEvents
from src.v2_live_writer import V2LiveWriter

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binding',type=Path,required=True);p.add_argument('--binding-sha256',required=True)
    p.add_argument('--execute-readonly',action='store_true');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();require(args.execute_readonly,'Explicit read-only preparation flag required')
    binding,cfg,storage=load_binding(args.binding,args.binding_sha256,ROOT)
    require(args.output.is_relative_to(ROOT) and args.output.parent.is_dir() and not args.output.exists(),
            'New private USB receipt required')
    from agilex_control.h50_journal import H50USBJournal
    with owned_session(cfg,storage=storage):
        journal=H50USBJournal(cfg,ROOT);before=can_counters();cache=None;writer=None;error=None
        tx=BoundedTxEvents();report={'status':'STARTED_READ_ONLY_PREFLIGHT','robot_motion_started':False,
            'motion_groups_sent':0,'source_binding':str(args.binding),'source_binding_sha256':args.binding_sha256}
        def guard():
            storage.verify_usb(cfg);check_stop(cfg,ROOT/'STOP-APPLY')
            if cache is not None:cache.check_alive()
        try:
            hub=ExistingHub(cfg,full_status=True)
            state=hub.status();require(not state['recording']['active'] and not state['recording']['saving'], 'Hub occupied')
            cache=FeedbackCache(hub).start();cache.wait_ready(timeout_s=3.)
            writer=V2LiveWriter(cfg,cache,tx,guard,canonical_root=binding['canonical_root'],
                canonical_sources=binding['canonical_sources'],confirmations={
                    'leader_isolated':True,'onsite_confirmed':True,'current_start_confirmed':True})
            report['writer_open']=writer.open();report['bounds_rad_m']=writer.bounds_rad_m
            require(writer.groups==0,'Preparation must never send a target group')
            report['status']='READ_ONLY_CONTROLLER_PREFLIGHT_COMPLETED'
        except BaseException as exc:
            error=exc;report.update(status='FAILED_RETAINED',error=type(exc).__name__+': '+str(exc))
        finally:
            if writer is not None:
                try:report['writer_close']=writer.close()
                except BaseException as exc:error=error or exc;report['writer_close_error']=str(exc)
            if cache is not None:
                while True:
                    try:report['feedback_cache']=cache.close(timeout_s=3.);break
                    except RuntimeError:continue
            report['before']=before;report['after']=can_counters();report['TX_events']=tx.records
            if error:report['status']='FAILED_RETAINED'
            try:journal.write_json(args.output,public_receipt(report))
            finally:journal.close()
        print(json.dumps({'status':report['status'],'report':str(args.output),'error':report.get('error'),
                          'bounds_rad_m':report.get('bounds_rad_m'),'motion_groups':0}))
        if error:raise error

if __name__=='__main__':main()
