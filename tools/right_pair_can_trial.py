# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Bounded, explicitly applied common-target trial for one OEM right-arm pair.

This does not alter production guards. It records anonymous bus feedback groups,
not independently identified physical arms. No reset, disable, calibration,
gripper command, automatic retry, or role restoration is permitted.
"""
from __future__ import annotations
from collections import deque
from contextlib import ExitStack
from enum import Enum
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import logging
import math
import select
import socket
import struct
import sys
import time

QIDS = (0x2A5, 0x2A6, 0x2A7)
DRIVERS = tuple(range(0x261, 0x267))
REQUIRED = QIDS + DRIVERS + (0x2A1,)
RXIDS = REQUIRED + (0x473, 0x476, 0x151, 0x155, 0x156, 0x157, 0x159, 0x470)
POSITION_LIMITS = ((-150, 150), (0, 180), (-170, 0),
                   (-100, 100), (-70, 70), (-120, 120))


class Blocked(RuntimeError):
    pass


def checked_q(q, limits=POSITION_LIMITS):
    if len(q) != 6 or any(not math.isfinite(v) or not lo <= v <= hi
                          for v, (lo, hi) in zip(q, limits)):
        raise Blocked("Joint target outside existing documented bounds")
    return list(q)


def stationary_groups(records, start, end):
    """Check up to two persistent value groups per joint-pair CAN ID.

    Groups are deliberately anonymous. The unchanged 0.5-degree stationarity
    bound is applied inside each group; a third group or temporal migration is
    rejected. The 2-degree common-target proximity bound is taken from the
    existing OEM handover contract. This is not physical source authentication.
    """
    if end-start < 0.5:
        raise Blocked("Insufficient stationary observation window")
    low, high, details = [], [], {}
    for cid in QIDS:
        points = [(t, q) for t, c, q in records if c == cid and start <= t <= end]
        groups = []
        for t, q in points:
            choices = [g for g in groups if max(abs(a-b) for a,b in zip(q, g[0][1])) <= 0.5]
            if choices:
                min(choices, key=lambda g: max(abs(a-b) for a,b in zip(q, g[0][1]))).append((t,q))
            elif len(groups) < 2:
                groups.append([(t,q)])
            else:
                raise Blocked("More than two stable value groups; source/motion ambiguous")
        if not groups:
            raise Blocked("Missing joint-pair feedback")
        for group in groups:
            if len(group) < 10 or group[0][0] > start+0.2 or group[-1][0] < end-0.2:
                raise Blocked("A feedback group is not persistent through the window")
            if any(max(q[j] for _,q in group)-min(q[j] for _,q in group) > 0.5 for j in (0,1)):
                raise Blocked("A feedback group exceeds 0.5-degree stationarity")
        bounds = [(min(q[j] for _,q in points),max(q[j] for _,q in points)) for j in (0,1)]
        if any(hi-lo > 2.0 for lo,hi in bounds):
            raise Blocked("Pair poses exceed existing 2-degree proximity bound")
        low.extend(lo for lo,hi in bounds)
        high.extend(hi for lo,hi in bounds)
        details[hex(cid)] = {"anonymous_group_count":len(groups),
                             "bounds_deg":bounds,"samples":len(points)}
    return checked_q([(lo+hi)/2 for lo,hi in zip(low,high)]), details


def allowed_frame(phase, cid, data, target=None):
    data = bytes(data)
    if len(data) != 8:
        return False
    if phase == "limits":
        return cid == 0x472 and data[0] in range(1,7) and data[1:] == bytes([1,0,0,0,0,0,0])
    if phase == "role":
        return cid == 0x470 and data == bytes.fromhex("fc00000000000000")
    if phase in ("track", "settle"):
        if cid == 0x151:
            return data == bytes.fromhex("01010a0000000000")
        if cid in (0x155,0x156,0x157) and target is not None:
            q = checked_q(target)
            index = (cid-0x155)*2
            return data == struct.pack(">ii", *(round(v*1000) for v in q[index:index+2]))
    return False


class Bus:
    class CAN_STATUS(Enum):
        SEND_MESSAGE_SUCCESS = 1

    def __init__(self, interface, stopped):
        self.socket = socket.socket(socket.PF_CAN,socket.SOCK_RAW,socket.CAN_RAW)
        self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024*1024)
        self.socket.setsockopt(socket.SOL_SOCKET,getattr(socket,"SO_TIMESTAMPNS",35),1)
        self.socket.setsockopt(socket.SOL_SOCKET,getattr(socket,"SO_RXQ_OVFL",40),1)
        self.socket.setsockopt(socket.SOL_CAN_RAW,socket.CAN_RAW_RECV_OWN_MSGS,0)
        self.socket.setsockopt(socket.SOL_CAN_RAW,socket.CAN_RAW_FILTER,
            b"".join(struct.pack("=II",c,0x7FF|0x80000000|0x40000000) for c in RXIDS))
        self.socket.setsockopt(socket.SOL_CAN_RAW,2,0x1FFFFFFF)
        self.socket.bind((interface,))
        self.socket.setblocking(False)
        self.latest = {}
        self.history = deque(maxlen=12000)
        self.raw_tail = deque(maxlen=2048)
        self.limit_replies = {}
        self.errors = []
        self.sent = {}
        self.stopped = stopped
        self.phase = "observe"
        self.target = None
        self.role_ack_count = 0
        self.unexpected_targets = 0
        self.last_read = {}
        self.started = time.monotonic()

    def poll(self, seconds=0):
        deadline = time.monotonic()+max(seconds, 0.005)
        while True:
            try:
                packet,ancillary,flags,_ = self.socket.recvmsg(16,128)
            except BlockingIOError:
                remaining=deadline-time.monotonic()
                if remaining <= 0 or seconds == 0: break
                select.select([self.socket],[],[],min(remaining,0.002))
                continue
            now=time.monotonic()
            kernel_ns=None
            for level,kind,value in ancillary:
                if level==socket.SOL_SOCKET and kind==getattr(socket,"SO_TIMESTAMPNS",35):
                    sec,ns=struct.unpack("=qq",value[:16]);kernel_ns=sec*1_000_000_000+ns
                if level==socket.SOL_SOCKET and kind==getattr(socket,"SO_RXQ_OVFL",40):
                    if struct.unpack("=I",value[:4])[0]:
                        self.errors.append("Receive socket overflow")
            if kernel_ns is None:
                self.errors.append("No kernel timestamp")
                continue
            age=(time.time_ns()-kernel_ns)/1e9
            stamp=now-age
            if not -0.005 <= age <= 0.2:
                self.errors.append("Stale/future kernel CAN receipt")
            if len(packet)!=16:
                self.errors.append("Malformed CAN packet");continue
            cid,dlc,data=struct.unpack("=IB3x8s",packet)
            self.raw_tail.append({"mono_s":stamp,"id":hex(cid),"data":data[:dlc].hex()})
            if cid & 0x20000000:
                self.errors.append("CAN error "+hex(cid)+":"+data.hex());continue
            if cid not in RXIDS or dlc!=8:
                self.errors.append("Unexpected CAN shape");continue
            if stamp < self.last_read.get(cid,float("-inf")):
                self.errors.append("Out-of-order CAN timestamp")
            self.last_read[cid]=stamp
            self.latest[cid]=(stamp,data)
            if cid in DRIVERS and data[5] != 0x40:
                self.errors.append("A raw driver feedback is disabled or faulted")
            if cid == 0x2A1 and (data[1] != 0 or data[3] not in (0,2,6) or data[6:8] != b"\0\0"):
                self.errors.append("A raw controller feedback reports fault/teaching")
            if cid in QIDS:
                self.history.append((stamp,cid,tuple(v/1000 for v in struct.unpack(">ii",data))))
            elif cid==0x473 and data[0] in range(1,7):
                hi,lo,_=struct.unpack(">hhH",data[1:7])
                self.limit_replies.setdefault(data[0],set()).add((lo/10,hi/10))
            elif cid==0x476 and data[0]==0x70 and self.phase!="observe":
                self.role_ack_count+=1
            elif cid in (0x151,0x155,0x156,0x157,0x159) and self.phase in ("track","settle"):
                # One socket sends and receives; its own echoes are disabled.
                self.unexpected_targets+=1
            if time.monotonic() >= deadline:
                break

    def healthy(self):
        if time.monotonic()-self.started > 25:
            raise Blocked("Trial absolute deadline exceeded")
        if self.stopped():
            raise Blocked("STOP is latched")
        if self.errors:
            raise Blocked(self.errors[0])
        if self.unexpected_targets:
            raise Blocked("Another target/mode transmitter observed after role request")
        now=time.monotonic()
        if any(c not in self.latest or not 0<=now-self.latest[c][0]<=.2 for c in REQUIRED):
            raise Blocked("Missing or stale raw feedback")
        s=self.latest[0x2A1][1]
        if s[1]!=0 or s[3] not in (0,2,6) or s[6:8]!=b"\0\0":
            raise Blocked("Controller fault or active button teaching/playback")
        if any(self.latest[c][1][5]!=0x40 for c in DRIVERS):
            raise Blocked("Driver enable/health not confirmed; no automatic enabling")

    def SendCanMessage(self,cid,data):
        self.healthy()
        if not allowed_frame(self.phase,cid,data,self.target):
            raise Blocked("Command outside reviewed phase allowlist")
        if cid==0x472 and self.sent.get("0x472",0)>=6:
            raise Blocked("Controller-limit query retry forbidden")
        if cid==0x470 and self.sent.get("0x470",0):
            raise Blocked("Role request retry forbidden")
        frame=struct.pack("=IB3x8s",cid,8,bytes(data))
        if self.socket.send(frame)!=16:
            raise Blocked("CAN send incomplete")
        key=hex(cid);self.sent[key]=self.sent.get(key,0)+1
        return self.CAN_STATUS.SEND_MESSAGE_SUCCESS

    def observe_window(self,seconds):
        start=time.monotonic()
        self.poll(seconds)
        self.healthy()
        return stationary_groups(list(self.history),start,time.monotonic())

    def observed_target_error(self,target,window=.15):
        now=time.monotonic()
        points=[(c,q) for t,c,q in self.history if now-window<=t<=now]
        if not all(any(c==cid for c,q in points) for cid in QIDS):
            raise Blocked("No recent joint-pair samples")
        return max(abs(q[j]-target[(c-0x2A5)*2+j]) for c,q in points for j in (0,1))


def run(bundle,pins,apply):
    bundle=Path(bundle).resolve(strict=True)
    contract=json.loads(Path(pins).read_text())
    if socket.gethostname()!=contract["hostname"] or Path("/proc/sys/kernel/random/boot_id").read_text().strip()!=contract["boot_id"]:
        raise Blocked("Host/boot contract mismatch")
    for relative,expected in contract["bundle_hashes"].items():
        if hashlib.sha256((bundle/relative).read_bytes()).hexdigest()!=expected:
            raise Blocked("Runtime source changed")
    if importlib.metadata.version("piper-sdk")!="0.6.2":
        raise Blocked("Unexpected SDK version")
    sdk_file=Path(importlib.metadata.distribution("piper-sdk").locate_file("piper_sdk/interface/piper_interface_v2.py"))
    if hashlib.sha256(sdk_file.read_bytes()).hexdigest()!=contract["sdk_sha256"]:
        raise Blocked("Installed SDK source changed")
    sys.path.insert(0,str(bundle))
    from agilex_control.storage import Ownership,Session,verify_usb,stop_requested
    from agilex_control.sensors import adapter_identity
    from agilex_control.hardware import plan_pose
    import urllib.request
    cfg=json.loads((bundle/"2026-10-06-14-05-config.json").read_text())
    verify_usb(cfg)
    identity=adapter_identity(cfg["can"]["right"]["interface"],cfg["can"]["right"]["expected_serial"])
    if identity["interface"]!=contract["interface"] or identity["serial"]!=contract["adapter_serial"]:
        raise Blocked("Right adapter identity mismatch")
    def idle():
        with urllib.request.urlopen(f'http://127.0.0.1:{cfg["hub_port"]}/status?compact=1',timeout=3) as r:
            h=json.load(r)
        if h["recording"].get("active") or h["recording"].get("saving") or h["task_state"].get("active"):
            raise Blocked("Another capture/task owner is active")
    idle()
    with Ownership("writer-"+identity["interface"]):
        idle()
        session=Session(cfg,"right-pair-can-trial")
        report=session.report
        report.update({"applied":apply,"physical_arm_identities_separately_verified":False,
                       "automatic_reset":False,"automatic_disable":False,"automatic_retry":False,
                       "source_contract":contract,"operator_authorized_scope":"right pair FC role then common six-zero target"})
        usb_checked = [0.0]
        def runtime_guard():
            if time.monotonic()-usb_checked[0] >= .2:
                verify_usb(cfg)
                usb_checked[0] = time.monotonic()
            return stop_requested(cfg)
        bus=Bus(identity["interface"],runtime_guard)
        try:
            q0,groups=bus.observe_window(1.2)
            report["preflight_groups"]=groups
            report["proposed_common_start_deg"]=q0
            if not apply:
                report["status"]="OFFLINE_PLAN_FROM_LIVE_RECEIVE_ONLY"
                return
            from piper_sdk import C_PiperInterface_V2
            # No SDK CAN socket, receive thread, ConnectPort or implicit queries.
            sdk=C_PiperInterface_V2(can_name=identity["interface"],can_auto_init=False,log_to_file=False)
            sdk._C_PiperInterface_V2__arm_can=bus
            bus.phase="limits"
            for joint in range(1,7):
                sdk.SearchMotorMaxAngleSpdAccLimit(joint,0x01)
                bus.poll(.06)
            bus.poll(.1);bus.healthy()
            if set(bus.limit_replies)!=set(range(1,7)):
                raise Blocked("Actual controller angle limits incomplete")
            limits=[]
            for i,documented in enumerate(POSITION_LIMITS,1):
                replies=bus.limit_replies[i]
                lo=max([documented[0]]+[x[0] for x in replies])
                hi=min([documented[1]]+[x[1] for x in replies])
                if lo>=hi:raise Blocked("Controller limit disagreement")
                limits.append((lo,hi))
            report["observed_common_limits_deg"]=limits
            q0,groups=bus.observe_window(.6)
            checked_q(q0,limits);checked_q([0]*6,limits)
            plan=plan_pose(q0,[0]*6,limits,max_speed_deg_s=5.0,max_accel_deg_s2=25.0)
            if plan.duration_s>15:
                raise Blocked("Trial exceeds bounded 15-second reference")
            report["plan"]=plan.as_dict()
            session.save()
            bus.phase="role"
            sdk.MasterSlaveConfig(0xFC,0,0,0)
            report["role_request_submitted"]=True
            bus.poll(.1);bus.healthy()
            if bus.observed_target_error(q0) > 2:
                raise Blocked("Unexpected displacement after role request")
            bus.phase="track"
            start=time.monotonic();tick=start
            while True:
                bus.poll();bus.healthy()
                elapsed=time.monotonic()-start
                target=list(plan.sample(elapsed))
                checked_q(target,limits)
                if bus.observed_target_error(target)>2:
                    raise Blocked("Observed common tracking error exceeds 2 degrees")
                bus.target=target
                sdk.MotionCtrl_2(0x01,0x01,10,0x00)
                sdk.JointCtrl(*(round(v*1000) for v in target))
                if elapsed>=plan.duration_s:break
                tick=max(tick+.02,time.monotonic())
                bus.poll(max(0,tick-time.monotonic()))
            bus.phase="settle"
            deadline=time.monotonic()+3
            zero_since=None
            while time.monotonic()<deadline:
                bus.poll();bus.healthy()
                error=bus.observed_target_error([0]*6,window=.5)
                if error<=2:
                    zero_since=zero_since or time.monotonic()
                    if time.monotonic()-zero_since>=.5:
                        endpoint_end=time.monotonic()
                        stationary_groups(list(bus.history),endpoint_end-.6,endpoint_end)
                        statuses=[row for row in bus.raw_tail if row["id"]=="0x2a1" and row["mono_s"]>=endpoint_end-.5]
                        if not statuses or any(bytes.fromhex(row["data"])[0:3]!=bytes([1,0,1]) or bytes.fromhex(row["data"])[4]!=0 for row in statuses):
                            raise Blocked("Not all recent controller frames confirm CAN/MOVEJ/arrival")
                        report["status"]="OBSERVED_BUS_ZERO_TARGET_REACHED_PAIR_VISUAL_CONFIRMATION_PENDING"
                        report["endpoint_max_observed_error_deg"]=error
                        break
                else:zero_since=None
                bus.target=[0]*6
                sdk.MotionCtrl_2(0x01,0x01,10,0x00)
                sdk.JointCtrl(0,0,0,0,0,0)
                bus.poll(.02)
            else:
                raise Blocked("Zero endpoint not confirmed in bounded settle window")
        except Exception as exc:
            report["status"]="BLOCKED_OR_ABORTED_NO_AUTOMATIC_RECOVERY"
            report["error"]=repr(exc)
        finally:
            report["packets_sent"]=dict(bus.sent)
            report["role_ack_count"]=bus.role_ack_count
            report["receive_errors"]=bus.errors[:20]
            report["unexpected_target_frames"]=bus.unexpected_targets
            report["raw_tail"]=list(bus.raw_tail)
            report["final_q_raw"]={hex(cid):bus.latest.get(cid,(None,b""))[1].hex() for cid in QIDS}
            report["completed_at_monotonic"]=time.monotonic()
            bus.socket.close()
            session.save()
            print(json.dumps({k:report.get(k) for k in
                ("status","error","packets_sent","role_request_submitted","proposed_common_start_deg",
                 "endpoint_max_observed_error_deg","role_ack_count")}|{"receipt":str(session.path/"operation.json")}))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle",required=True)
    parser.add_argument("--pins",required=True)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    run(args.bundle,args.pins,args.apply)
