"""Pure controller-mode presentation from real eight-byte 0x2A1 feedback.

Called by the web cache after its ages have advanced. No SDK, socket, query,
motion or filesystem access occurs here. A requested mode is never feedback.
"""
import math
import copy

from .mode_evidence import summarize_mode_receipt

EXTERNAL_ACTIVITY_WINDOW_S = 5.0


def _age(value):
    return (float(value) if type(value) in (int, float) and
            math.isfinite(value) and value >= 0 else None)


def _reported_mode(fields):
    """Decode known raw bytes without claiming they are current feedback."""
    if fields["err_code"] != 0 or fields["arm_status"] not in (0, 0x0B, 0x0C, 0x0D):
        return "FAULT", "控制器异常", "真实 arm_status 或 err_code 非零"
    if (fields["ctrl_mode"] in (2, 5, 6, 7) or fields["arm_status"] in (0x0B, 0x0C, 0x0D)
            or fields["teach_status"] not in (0, 2, 6)):
        return "TEACH", "示教／轨迹状态", "示教、遥控、联动或轨迹流程仍在占用；结束值 2、终止值 6 单独视为合法"
    if fields["ctrl_mode"] == 0:
        return "STANDBY", "待机 STANDBY", "真实反馈为待机，尚未接管 CAN 关节控制"
    if fields["ctrl_mode"] == 1 and fields["mode_feed"] == 1:
        return "CAN_MOVEJ", "CAN / MOVEJ", "已收到 CAN 关节运动模式反馈；不等于已验证独占控制或普通子模式"
    return "OTHER", "其他控制模式", "当前模式不属于本版普通 CAN/MOVEJ 接管目标"


def controller_mode(snapshot):
    """Return an honest display/gating view; unknown bytes stay unknown."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    age = _age(snapshot.get("age_status_s"))
    threshold = _age(snapshot.get("stale_after_s"))
    threshold = .2 if threshold is None else threshold
    raw_hex = snapshot.get("status_hex")
    fields, malformed = {}, False
    if raw_hex is not None:
        try:
            raw = bytes.fromhex(raw_hex)
            if len(raw) != 8:
                raise ValueError("eight bytes required")
            fields = dict(ctrl_mode=raw[0], arm_status=raw[1], mode_feed=raw[2],
                          teach_status=raw[3], motion_status=raw[4],
                          trajectory_num=raw[5], err_code=int.from_bytes(raw[6:], "big"))
        except (TypeError, ValueError):
            malformed = True
    decoded = snapshot.get("status")
    mismatch = bool(fields and isinstance(decoded, dict) and
                    any(k in decoded and decoded[k] != v for k, v in fields.items()))
    fresh = bool(fields and age is not None and age <= threshold and not malformed and
                 not mismatch and not snapshot.get("observer_error") and
                 not snapshot.get("timestamp_failures"))
    motor_age = _age(snapshot.get("age_motor_s"))
    enabled = snapshot.get("enabled")
    if not isinstance(enabled, list) or len(enabled) != 6 or any(type(v) is not bool for v in enabled):
        enabled = None
    enabled_fresh = enabled is not None and motor_age is not None and motor_age <= threshold
    external, activity = [], []
    for cid, row in (snapshot.get("external_commands") or {}).items():
        if not isinstance(row, dict):
            continue
        command_age = _age(row.get("last_age_s"))
        count = row.get("count")
        if (type(count) is not int or count <= 0 or command_age is None
                or command_age > max(threshold, EXTERNAL_ACTIVITY_WINDOW_S)):
            continue
        item = dict(can_id=cid, count=count, last_age_s=command_age,
                    payload_hex=row.get("payload_hex"), origin="总线观测，发送者未识别")
        if cid.lower() == "0x151":
            try:
                command = bytes.fromhex(row.get("payload_hex", ""))
                if len(command) == 8:
                    item["requested_submode"] = command[3]
                    item["high_follow_observed"] = command[3] == 0xAD
            except (TypeError, ValueError):
                pass
        if command_age <= threshold:
            external.append(item)
        if command_age <= EXTERNAL_ACTIVITY_WINDOW_S:
            activity.append(dict(item))
    classification, label, reason = "UNKNOWN", "模式未知", "尚无完整、可核对的 0x2A1 原始反馈"
    raw_valid = bool(fields and not malformed and not mismatch)
    reported = _reported_mode(fields) if raw_valid else ("UNKNOWN", "模式未知", reason)
    if mismatch:
        reason = "原始状态与解码字段不一致，拒绝宣称模式已确认"
    elif malformed:
        reason = "0x2A1 载荷不是完整的八字节十六进制数据"
    elif fields and not fresh:
        classification, label, reason = "STALE", "反馈已过期", "保留最后状态供诊断；不代表当前模式"
    elif fresh:
        classification, label, reason = reported
    blocked = None
    if classification not in ("STANDBY", "CAN_MOVEJ"):
        blocked = reason
    elif external:
        blocked = "近期存在总线控制报文；先核实发送来源与隔离，不能通过勾选消除"
    return dict(classification=classification, label=label, reason=reason,
                last_reported_classification=reported[0], last_reported_label=reported[1],
                last_reported_valid=raw_valid,
                last_reported_note="最后收到且字段一致的原始 0x2A1 解码；当前是否有效另见 fresh/age_status_s，不用于替代执行核验",
                raw_status_hex=raw_hex, **fields, interface=snapshot.get("interface"),
                fresh=fresh, age_status_s=age, enabled=enabled,
                enabled_fresh=enabled_fresh, age_low_s=motor_age,
                source_wall_time_s=snapshot.get("status_receive_wall_time_s"),
                enable_source_wall_time_s=snapshot.get("motor_receive_wall_time_s"),
                receipt_monotonic_s=snapshot.get("status_receipt_monotonic_s"),
                external_recent=external, external_activity=activity,
                external_activity_window_s=EXTERNAL_ACTIVITY_WINDOW_S,
                external_activity_active=bool(activity),
                external_activity_note="近 5 秒实际观测到的控制报文；年龄按真实接收时间增长，累计计数不延长窗口；未观测到不证明物理隔离",
                is_mit_mode=None,
                submode_feedback_available=False,
                submode_note="0x2A1 不包含 is_mit_mode；普通位置速度子模式只能显示请求，不能由此证明",
                position_velocity_feedback_available=False,
                switchability={"available": blocked is None, "reason": blocked})


def controller_modes(value):
    """Use current direct observer state, never a requested result as actual."""
    arms = value.get("status", {}).get("arms", {})
    result = {role: controller_mode(arms.get(role)) for role in ("right", "left")}
    for job in reversed(value.get("jobs", [])):
        if job.get("action") not in ("mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable"):
            continue
        role = job.get("params", {}).get("arm", "right")
        if role not in result or "requested_mode" in result[role]:
            continue
        receipt = job.get("mode_receipt") or (job.get("result") or {}).get("mode_receipt")
        if not receipt:
            receipt = (job.get("result") or {}).get("result", {})
        result[role]["requested_mode"] = dict(action=job["action"], job_state=job.get("state"),
            receipt=receipt, job_id=job.get("id"), created_at=job.get("created_at"),
            finished_at=job.get("finished_at"), report=job.get("report"),
            note="最后显式任务回执；当前真实状态仍取自独立 CAN 反馈")
    return result


def compact_mode_receipt(receipt):
    """Small SSH/UI receipt; full bounded evidence stays in USB operation.json.

    Avoid losing an entire JSON receipt to the subprocess output ring buffer.
    The chosen evidence remains unmodified; omitted detail is stated explicitly.
    """
    if not isinstance(receipt, dict):
        return None
    scalar_keys = ("operation", "applied", "status", "completed", "controller_mode_confirmed",
                   "reset_submitted", "reset_attempted", "recovery_confirmed", "targets_sent", "enabled_by_operation",
                   "packets_sent", "failure_code", "failure", "limitation", "requested_mode",
                   "engineering_bounds", "CAN_commands_sent", "SDK_constructed", "manual_guidance",
                   "elapsed_s", "sample_count", "samples_truncated", "request_count",
                   "mode_was_sustained_while_requesting", "maximum_request_gap_s",
                   "preflight_joint_span_deg", "recovery_joint_span_deg", "reset_packets_sent",
                   "reset_submitted_monotonic_s", "last_request_monotonic_s",
                   "standby_feedback_span_s", "standby_distinct_feedback_count",
                   "can_movej_feedback_span_s", "can_movej_distinct_feedback_count", "cancelled")
    scalar_keys += ("steps", "required_confirmations", "standby_first_request_monotonic_s",
                    "can_movej_first_request_monotonic_s")
    scalar_keys += ("motor_disable_confirmed", "physical_teleoperation_confirmed", "first_request_monotonic_s",
                    "can_movej_feedback_confirmed",
                    "confirmation_feedback_span_s", "confirmation_feedback_count", "confirmation_distinct_count", "before_oem_source",
                    "last_oem_source", "preflight_oem_target_span_deg", "roles_configured_by_operation",
                    "gripper_disabled_by_operation")
    result = {key: copy.deepcopy(receipt[key]) for key in scalar_keys if key in receipt}

    def sample(row):
        if not isinstance(row, dict):
            return None
        return {key: copy.deepcopy(row[key]) for key in
                ("phase", "observed_monotonic_s", "received_monotonic_s", "status_hex", "status",
                 "q_deg", "enabled", "motor_flags", "feedback_ages_s", "observer_error",
                 "timestamp_failures", "feedback_error") if key in row}

    result["before"], result["after"] = sample(receipt.get("before")), sample(receipt.get("after"))
    rows = receipt.get("samples")
    if not isinstance(rows, list):
        rows = receipt.get("recent_status_samples")
    rows = rows if isinstance(rows, list) else []
    result["recent_status_samples"] = [sample(row) for row in rows[-8:]]
    problems = receipt.get("observed_recovery_problems")
    result["observed_recovery_problems"] = copy.deepcopy(problems[-4:]) if isinstance(problems, list) else []
    previous_summary = receipt.get("evidence_summary")
    result["evidence_summary"] = (copy.deepcopy(previous_summary)
        if receipt.get("ui_receipt_compacted") is True and isinstance(previous_summary, dict)
        and previous_summary.get("schema_version") == 1 else summarize_mode_receipt(receipt))
    result["ui_receipt_compacted"] = True
    result["details_in_usb_report"] = True
    return result
