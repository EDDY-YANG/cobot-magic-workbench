# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Restore one physically isolated right leader with exactly one FA request.

Requires explicit on-site isolation confirmation. Sends only one firmware query
and one FA role configuration. Never enables, disables, resets, homes, or sends
joint/gripper targets. Raw evidence is USB-bound.
"""
from pathlib import Path
from enum import Enum
import argparse
import hashlib
import importlib.metadata
import json
import socket
import struct
import sys
import time
import urllib.request

FRAME_FILTER = (0x4AF,0x470,0x476,0x2A1,0x2A5,0x2A6,0x2A7,0x151,0x155,0x156,0x157,0x159)


class Transport:
    class CAN_STATUS(Enum):
        SEND_MESSAGE_SUCCESS = 1

    def __init__(self, sock, guard):
        self.sock,self.guard = sock,guard
        self.sent = {}
        self.phase = "query"

    def SendCanMessage(self, can_id, data):
        self.guard()
        payload=bytes(data)
        expected=(0x4AF,bytes.fromhex("0100000000000000")) if self.phase=="query" else (0x470,bytes.fromhex("fa00000000000000"))
        if (can_id,payload)!=expected or self.sent.get(hex(can_id),0):
            raise RuntimeError("Command/retry outside isolated-leader restore contract")
        if self.sock.send(struct.pack("=IB3x8s",can_id,8,payload))!=16:
            raise RuntimeError("Incomplete CAN submission")
        self.sent[hex(can_id)]=1
        return self.CAN_STATUS.SEND_MESSAGE_SUCCESS


def receive(sock, seconds, destination):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        try:packet=sock.recv(16)
        except socket.timeout:continue
        cid,length,payload=struct.unpack("=IB3x8s",packet)
        record={"mono_s":time.monotonic(),"id":hex(cid),"data":payload[:length].hex()}
        destination.append(record)
        if cid & 0x20000000:
            raise RuntimeError("CAN error during isolated restore: "+str(record))
        if length!=8 or cid not in FRAME_FILTER:
            raise RuntimeError("Unexpected CAN frame")
        if len(destination)>4096:
            raise RuntimeError("Bounded capture exceeded")


def run(bundle, confirmed):
    if not confirmed:raise RuntimeError("On-site isolation confirmation is required")
    bundle=Path(bundle).resolve(strict=True)
    sys.path.insert(0,str(bundle))
    from agilex_control.storage import Ownership,Session,verify_usb,stop_requested
    from agilex_control.sensors import adapter_identity
    cfg=json.loads((bundle/"2026-10-06-14-05-config.json").read_text())
    verify_usb(cfg)
    if importlib.metadata.version("piper-sdk")!="0.6.2":
        raise RuntimeError("SDK changed")
    sdk_path=Path(importlib.metadata.distribution("piper-sdk").locate_file("piper_sdk/interface/piper_interface_v2.py"))
    expected="1dbdbfd8eb133542634c40356e2096efe08c4c4c47501b13808cdd20661800e5"
    if hashlib.sha256(sdk_path.read_bytes()).hexdigest()!=expected:
        raise RuntimeError("SDK source changed")
    identity=adapter_identity(cfg["can"]["right"]["interface"],cfg["can"]["right"]["expected_serial"])
    with Ownership("writer-"+identity["interface"]):
        with urllib.request.urlopen(f'http://127.0.0.1:{cfg["hub_port"]}/status?compact=1',timeout=3) as r:
            hub=json.load(r)
        if hub["recording"].get("active") or hub["recording"].get("saving") or hub["task_state"].get("active"):
            raise RuntimeError("Existing owner is active")
        session=Session(cfg,"isolated-right-leader-restore")
        report=session.report
        report.update({"operator_confirmed_isolation":True,"target_physical_label":"right leader",
                       "robot_boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                       "adapter":identity,"sdk_version":"0.6.2","sdk_source_sha256":expected,
                       "motion_targets_sent":0,"enable_disable_reset_commands_sent":0,
                       "raw_before":[],"raw_query":[],"raw_after":[]})
        transport=None
        with socket.socket(socket.PF_CAN,socket.SOCK_RAW,socket.CAN_RAW) as bus:
            bus.setsockopt(socket.SOL_CAN_RAW,socket.CAN_RAW_FILTER,
                b"".join(struct.pack("=II",cid,0x7FF|0x80000000|0x40000000) for cid in FRAME_FILTER))
            bus.setsockopt(socket.SOL_CAN_RAW,socket.CAN_RAW_RECV_OWN_MSGS,0)
            bus.setsockopt(socket.SOL_CAN_RAW,2,0x1FFFFFFF)
            bus.settimeout(.02)
            bus.bind((identity["interface"],))
            def guard():
                verify_usb(cfg)
                if stop_requested(cfg):raise RuntimeError("STOP is latched")
            transport=Transport(bus,guard)
            try:
                guard()
                receive(bus,.5,report["raw_before"])
                counts={cid:sum(f["id"]==hex(cid) for f in report["raw_before"]) for cid in (0x2A1,0x2A5,0x2A6,0x2A7)}
                if any(not 50<=count<=140 for count in counts.values()):
                    raise RuntimeError("Feedback does not match one isolated 200 Hz output arm")
                report["before_counts"]=counts
                from piper_sdk import C_PiperInterface_V2
                sdk=C_PiperInterface_V2(can_name=identity["interface"],can_auto_init=False,log_to_file=False)
                sdk._C_PiperInterface_V2__arm_can=transport
                sdk.SearchPiperFirmwareVersion()
                receive(bus,.6,report["raw_query"])
                firmware=b"".join(bytes.fromhex(f["data"]) for f in report["raw_query"] if f["id"]=="0x4af")
                index=firmware.find(b"S-V")
                value=firmware[index:index+8].decode("ascii",errors="replace") if index>=0 else None
                report["firmware_version"]=value
                if value!="S-V1.9-1":
                    raise RuntimeError("Isolated arm firmware differs from reviewed right-side version")
                session.save()
                transport.phase="role"
                report["role_request_mono_s"]=time.monotonic()
                sdk.MasterSlaveConfig(0xFA,0,0,0)
                report["role_request_submitted"]=True
                receive(bus,1.0,report["raw_after"])
                ack=[f for f in report["raw_after"] if f["id"]=="0x476" and bytes.fromhex(f["data"])[0]==0x70]
                report["role_ack_count"]=len(ack)
                report["new_control_frames"]={hex(cid):sum(f["id"]==hex(cid) for f in report["raw_after"]) for cid in (0x151,0x155,0x156,0x157,0x159)}
                report["status"]="ROLE_ACKNOWLEDGED_PAIR_RECONNECT_PENDING" if ack else "ROLE_SUBMITTED_ACK_NOT_OBSERVED"
            except Exception as exc:
                report["status"]="RESTORE_BLOCKED_OR_UNCONFIRMED"
                report["error"]=repr(exc)
            finally:
                report["packets_sent"]=dict(transport.sent)
                session.save()
                print(json.dumps({k:report.get(k) for k in ("status","error","firmware_version","role_request_submitted","role_ack_count","new_control_frames","packets_sent")}|{"receipt":str(session.path/"operation.json")},ensure_ascii=False))


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle",required=True)
    p.add_argument("--isolated-right-leader-confirmed",action="store_true")
    args=p.parse_args()
    run(args.bundle,args.isolated_right_leader_confirmed)
