# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Select registered, independently identified CAN/MOVEJ arms for joint zero.

No device access in this module. The CLI rechecks under all writer locks.
"""
import math
from .mode_state import controller_mode
from .hardware import DOCUMENTED_COMMAND_LIMITS_DEG, validate_target_deg

def selection(cfg, states):
    entries = cfg.get("can", {})
    if not entries:
        raise ValueError("No registered CAN arms")
    identities = [(v.get("interface"), v.get("expected_serial")) for v in entries.values()]
    if (any(not a or not b for a,b in identities)
            or len({a for a,b in identities}) != len(identities)
            or len({b for a,b in identities}) != len(identities)):
        raise ValueError("CAN arm identities must have unique interfaces and USB serials")
    targets, skipped, blocked = [], {}, {}
    for role in sorted(entries):
        state = states.get(role, {})
        mode = controller_mode(state)
        if not mode.get("fresh"):
            skipped[role] = mode.get("reason") or "Mode feedback unavailable"
            continue
        if mode.get("ctrl_mode") != 1 or mode.get("mode_feed") != 1:
            skipped[role] = mode.get("label") or "Not CAN/MOVEJ"
            continue
        targets.append(role)
        reason = None
        identity = state.get("identity", {})
        if (state.get("interface") != entries[role]["interface"]
                or identity.get("identity_verified") is not True
                or identity.get("serial") != entries[role]["expected_serial"]):
            reason = "Actual CAN adapter identity does not match the registered arm"
        elif state.get("fresh") is not True or state.get("observer_error") or state.get("timestamp_failures"):
            reason = "Joint feedback unavailable or invalid"
        elif mode.get("classification") != "CAN_MOVEJ":
            reason = "Controller fault or teaching state"
        elif mode.get("enabled_fresh") is not True or mode.get("enabled") != [True]*6:
            reason = "All six motors must already be enabled; no automatic enable"
        elif mode.get("external_activity"):
            reason = "Another command source was observed"
        else:
            flags = state.get("motor_flags")
            if not isinstance(flags,list) or len(flags)!=6 or any(type(x) is not int or x & 0xBF for x in flags):
                reason = "Motor fault flags unavailable or abnormal"
            else:
                try:
                    validate_target_deg(state.get("q_deg"), DOCUMENTED_COMMAND_LIMITS_DEG)
                except (ValueError, RuntimeError, TypeError) as exc:
                    reason = str(exc)
        if reason:
            blocked[role] = reason
    return {"schema":"registered-can-zero-v1","targets":targets,"skipped":skipped,
            "blocked":blocked,"registered":sorted(entries),"sequence":targets,
            "target_deg":[0.0]*6,"gripper":"unchanged","execution":"sequential",
            "scope":"Registered independently identified arms only"}

def require_selection(cfg, states, expected):
    result = selection(cfg, states)
    if not isinstance(expected,list) or not expected or len(set(expected)) != len(expected):
        raise ValueError("A nonempty frozen target-arm list is required")
    if sorted(expected) != result["targets"]:
        raise ValueError("CAN/MOVEJ arm set changed; refresh the target list before retrying")
    if result["blocked"]:
        raise ValueError("CAN/MOVEJ arm preflight blocked: "+str(result["blocked"]))
    return result

def view(value, cfg, connected):
    try:
        result = selection(cfg, value.get("status",{}).get("arms",{}))
        reason = None
        if not connected: reason = "请先连接机械臂"
        elif value.get("usb",{}).get("verified") is not True: reason = "U盘身份未核验"
        elif value.get("stop_latch",{}).get("latched") is not False: reason = "停止锁存或其状态未知"
        elif any(j.get("state") in ("queued","running","unknown") for j in value.get("jobs",[])): reason = "当前任务未结束"
        elif not result["targets"]: reason = "当前没有已确认处于 CAN/MOVEJ 的已登记机械臂"
        elif result["blocked"]: reason = "六零位预检阻挡："+str(result["blocked"])
        return {**result,"available":reason is None,"reason":reason}
    except (ValueError,RuntimeError,TypeError,KeyError) as exc:
        return {"available":False,"reason":str(exc),"targets":[],"skipped":{},"blocked":{}}
