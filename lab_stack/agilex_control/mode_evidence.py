"""Pure historical mode-receipt explanation; never a control/gating decision.

No SDK, network, hardware or filesystem imports. Counts describe only retained
raw samples. A previous successful operation is never a current robot state.
"""
import math
from collections import Counter


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _clock_evidence(receipt, rows):
    """Check supplied identities only; old receipts have no domain identity.

    A receive timestamp is not globally comparable across hosts, boots or
    capture sessions. Never infer those identities from numeric timestamps.
    This is a diagnostic check, not a new controller execution condition.
    """
    identity_fields = ("host_id", "boot_id", "session_id", "clock_name")
    values = {}

    def add(key, value):
        if isinstance(value, str) and value.strip():
            values.setdefault(key, set()).add(value.strip())

    def inspect(record):
        if not isinstance(record, dict):
            return False
        supplied = set()
        for key in identity_fields:
            value = record.get(key)
            add(key, value)
            if isinstance(value, str) and value.strip():
                supplied.add(key)
        domain = record.get("clock_domain")
        if isinstance(domain, dict):
            for key in identity_fields:
                value = domain.get(key)
                add(key, value)
                if isinstance(value, str) and value.strip():
                    supplied.add(key)
        else:
            add("domain_id", domain)
        return all(key in supplied for key in identity_fields)

    receipt_complete = inspect(receipt)
    row_identities = []
    for row in rows:
        row_complete = inspect(row)
        raw_complete = inspect(row.get("raw_status")) if isinstance(row, dict) else False
        row_identities.append(row_complete or raw_complete)
    conflicts = sorted(key for key, identities in values.items() if len(identities) > 1)
    complete = not conflicts and (receipt_complete or bool(rows) and all(row_identities))
    return dict(status="MISMATCH" if conflicts else "CONSISTENT_EXPLICIT_IDENTITY" if complete else "UNKNOWN",
                identity_available=complete, conflicting_fields=conflicts,
                observed_fields={key: sorted(identities) for key, identities in values.items()},
                note="仅核对回执显式提供的时钟身份；缺失字段保持未知，不证明旧回执同域，也不改变执行门控。")


def _decode_sample(row):
    if not isinstance(row, dict):
        return None, "invalid_sample"
    stamp = row.get("received_monotonic_s")
    if not _number(stamp):
        return None, "missing_source_timestamp"
    if row.get("observer_error") or row.get("timestamp_failures") or row.get("feedback_error"):
        return None, "reported_feedback_error"
    try:
        raw = bytes.fromhex(row.get("status_hex"))
        if len(raw) != 8:
            raise ValueError("eight bytes required")
    except (TypeError, ValueError):
        return None, "invalid_raw_payload"
    observed = row.get("observed_monotonic_s")
    if _number(observed) and stamp > observed:
        return None, "source_after_observation"
    raw_status = row.get("raw_status")
    if raw_status is not None:
        if (not isinstance(raw_status, dict) or raw_status.get("monotonic_s") != stamp
                or raw_status.get("payload_hex") != row.get("status_hex")):
            return None, "raw_source_mismatch"
    fields = dict(ctrl_mode=raw[0], arm_status=raw[1], mode_feed=raw[2],
                  teach_status=raw[3], motion_status=raw[4], trajectory_num=raw[5],
                  err_code=int.from_bytes(raw[6:], "big"))
    decoded = row.get("status")
    if isinstance(decoded, dict) and any(k in decoded and decoded[k] != v for k, v in fields.items()):
        return None, "decoded_raw_mismatch"
    if fields["err_code"] or fields["arm_status"] != 0:
        mode = "FAULT_OR_OTHER"
    elif fields["teach_status"] not in (0, 2, 6):
        mode = "TEACH_OR_OTHER"
    elif fields["ctrl_mode"] == 0:
        mode = "STANDBY"
    elif fields["ctrl_mode"] == 1 and fields["mode_feed"] == 1:
        mode = "CAN_MOVEJ"
    else:
        mode = "OTHER"
    return dict(stamp=stamp, raw=raw.hex(), mode=mode, phase=row.get("phase")), None


def summarize_mode_receipt(receipt):
    """Explain retained evidence, preserving uncertainty and receipt outcome.

    ``sample_count`` counts collector observations (including phase changes),
    so it must never be presented as total distinct CAN frames. Counter values
    under external_commands are cumulative snapshots and must not be summed.
    """
    receipt = receipt if isinstance(receipt, dict) else {}
    rows = receipt.get("samples")
    source = "samples"
    if not isinstance(rows, list):
        rows, source = receipt.get("recent_status_samples"), "recent_status_samples"
    rows = rows if isinstance(rows, list) else []
    clock = _clock_evidence(receipt, rows)
    clock_conflict = bool(clock["conflicting_fields"])
    invalid, by_stamp, conflict_stamps = Counter(), {}, set()
    duplicate_rows, external_max, external_recent = 0, {}, set()
    external_inspected_rows = 0
    for row in rows:
        decoded, error = _decode_sample(row)
        if error:
            invalid[error] += 1
        elif decoded["stamp"] in by_stamp:
            previous = by_stamp[decoded["stamp"]]
            if previous["raw"] != decoded["raw"]:
                conflict_stamps.add(decoded["stamp"])
            else:
                duplicate_rows += 1
                # Phase changes can repeat a receive timestamp; not a new frame.
                if decoded["phase"] == "can_movej":
                    previous["phase"] = "can_movej"
        else:
            by_stamp[decoded["stamp"]] = decoded
        commands = row.get("external_commands") if isinstance(row, dict) else None
        if not isinstance(commands, dict):
            continue
        counters_inspected = False
        for cid, activity in commands.items():
            if not isinstance(cid, str) or not isinstance(activity, dict):
                continue
            count, age = activity.get("count"), activity.get("last_age_s")
            if type(count) is int and count >= 0:
                counters_inspected = True
            if type(count) is int and count > 0:
                external_max[cid] = max(external_max.get(cid, 0), count)
                if _number(age) and age <= 5:
                    external_recent.add(cid)
        if counters_inspected:
            external_inspected_rows += 1
    for stamp in conflict_stamps:
        by_stamp.pop(stamp, None)
    # Sorting mixed clocks would invent mode reversions and a fictitious span.
    samples = [] if clock_conflict else sorted(by_stamp.values(), key=lambda row: row["stamp"])
    counts = Counter(row["mode"] for row in samples)
    request_samples = [row for row in samples if row["phase"] == "can_movej"]
    request_counts = Counter(row["mode"] for row in request_samples)
    request_reversions = sum(a["mode"] == "CAN_MOVEJ" and b["mode"] == "STANDBY"
                             for a, b in zip(request_samples, request_samples[1:]))
    request_transitions = sum(a["mode"] != b["mode"] for a, b in zip(request_samples, request_samples[1:]))
    transitions = sum(a["mode"] != b["mode"] for a, b in zip(samples, samples[1:]))
    can_standby_transitions = sum({a["mode"], b["mode"]} == {"CAN_MOVEJ", "STANDBY"}
                                 for a, b in zip(samples, samples[1:]))
    total = receipt.get("sample_count")
    total = total if type(total) is int and total >= 0 else None
    truncated = bool(receipt.get("samples_truncated") or source == "recent_status_samples"
                     or (total is not None and total > len(rows)))
    scope_kind = ("NO_SAMPLES" if not rows else "RETAINED_WINDOW" if truncated else
                  "COMPLETE_RECORDED_SAMPLE_SET" if total == len(rows) else "STORED_SAMPLE_WINDOW")
    scope_label = {"NO_SAMPLES": "未保存原始样本，只有操作结果摘要",
                   "RETAINED_WINDOW": "仅保留窗口，不代表全程",
                   "COMPLETE_RECORDED_SAMPLE_SET": "完整保存的观测样本，非总线全部帧",
                   "STORED_SAMPLE_WINDOW": "保留样本，全程覆盖未知"}[scope_kind]
    status, operation = receipt.get("status"), receipt.get("operation")
    if operation is None and status == "CAN_MOVEJ_CONFIRMED_WITH_BOUNDED_POST_OBSERVATION":
        operation = "takeover"
    confirmed = receipt.get("controller_mode_confirmed") is True
    common_next = "保留回执并核对实时反馈、发送来源和交接条件；本摘要不触发复位、使能或运动。"
    if clock_conflict:
        code, title = "EVIDENCE_CLOCK_DOMAIN_MISMATCH", "回执混入不同来源的时钟身份，无法合并统计"
        detail = "主机、启动、采集会话或时钟域字段存在显式冲突；不排序合并模式变化，也不据成功标志确认该份证据。"
    elif receipt.get("applied") is False:
        code, title = "PLAN_ONLY", "仅生成计划，未执行模式操作"
        detail = "计划参数不属于控制器实际反馈。"
    elif operation == "reset":
        submitted = receipt.get("reset_submitted") is True
        recovered = receipt.get("recovery_confirmed") is True
        code = "RESET_RECOVERY_REPORTED" if submitted and recovered else "RESET_NOT_CONFIRMED"
        title = "单次复位及反馈恢复已记录" if submitted and recovered else "复位或反馈恢复未完整确认"
        detail = "复位结果与 CAN/MOVEJ 接管结果分开；不代表已使能或可以运动。"
    elif conflict_stamps or invalid.get("decoded_raw_mismatch") or invalid.get("raw_source_mismatch"):
        code, title = "EVIDENCE_INCONSISTENT", "回执中的原始反馈证据不一致"
        detail = "同一来源时刻出现冲突，或原始载荷与解码字段不符；不据此确认模式。"
    elif confirmed and status == "CAN_MOVEJ_CONFIRMED_WITH_BOUNDED_POST_OBSERVATION":
        code, title = "TAKEOVER_REPORTED_CONFIRMED", "该次模式交接已获反馈确认（历史）"
        detail = "历史回执报告连续确认及请求后观察通过；不证明内部子模式或电机目标已执行，当前模式仍取新的独立反馈。"
    elif request_reversions:
        code = "UNSTABLE_CAN_STANDBY" if request_transitions > 1 else "CAN_REVERTED_TO_STANDBY"
        title = ("已收到 CAN 与待机交替反馈，接管未稳定" if request_transitions > 1 else
                 "收到 CAN 后又回到待机，接管未确认")
        detail = "请求阶段保留样本中出现 CAN 后回待机；不能把单帧 CAN 当成交接完成，也不能据此认定主臂抢占。"
    elif request_counts["CAN_MOVEJ"]:
        code, title = "CAN_OBSERVED_NOT_CONFIRMED", "已收到 CAN 反馈，连续接管仍未确认"
        detail = "保留的请求阶段有 CAN/MOVEJ，但回执没有报告完整交接成功；需结合失败码和完整时序排查。"
    elif request_samples:
        code, title = "NO_CAN_IN_RETAINED_REQUEST_WINDOW", "保留的请求窗口内未见 CAN/MOVEJ"
        detail = "仅描述保留窗口，不能推断被截断的更早反馈，也不能从发送数量判断控制器已接管。"
    elif samples:
        code, title = "NO_REQUEST_PHASE_EVIDENCE", "尚无可核对的请求阶段反馈"
        detail = "只保存了其他阶段的原始反馈；无法据此判断请求后是否进入 CAN/MOVEJ。"
    else:
        code, title = "NO_VALID_FEEDBACK_EVIDENCE", "未保存可核对的原始反馈样本"
        detail = "反馈载荷、来源时间戳或一致性证据不足；不把请求参数当成实际模式。"
    ids = sorted(external_max)[:64]
    engineering_bounds = receipt.get("engineering_bounds")
    engineering_bounds = engineering_bounds if isinstance(engineering_bounds, dict) else {}
    return dict(
        schema_version=1, code=code, title=title, detail=detail, next_step=common_next,
        historical_only=True, affects_execution_gate=False,
        scope=dict(kind=scope_kind, source=source, retained_rows=len(rows), unique_valid_source_timestamps=len(samples),
                   reported_observation_count=total, truncated=truncated,
                   label=scope_label, clock_domain=clock,
                   note="sample_count 是采集器观测累计数，不等于全程唯一 CAN 帧数。"),
        feedback=dict(counts=dict(counts), request_phase_counts=dict(request_counts),
                      transitions=transitions, can_standby_transitions=can_standby_transitions,
                      request_can_to_standby_reversions=request_reversions,
                      observed_span_s=(samples[-1]["stamp"]-samples[0]["stamp"] if len(samples)>1 else 0),
                      duplicate_rows=duplicate_rows, conflicting_source_timestamps=len(conflict_stamps),
                      invalid_rows=sum(invalid.values()), invalid_reasons=dict(invalid)),
        external_commands=dict(evidence_available=external_inspected_rows > 0,
                               inspected_sample_rows=external_inspected_rows,
                               observed_positive_counters=(bool(external_max) if external_inspected_rows else None),
                               positive_counter_max_by_id={cid: external_max[cid] for cid in ids},
                               recent_ids_in_retained_samples=sorted(external_recent)[:64],
                               sender_identified=False, ids_truncated=len(external_max)>64,
                               note="缺少计数证据时为未知，不等于零；计数大于零才是观测记录，累计计数不求和。CAN ID 不等于设备身份，零计数不证明物理隔离。"),
        request=dict(operation=operation, status=status, failure_code=receipt.get("failure_code"),
                     controller_mode_confirmed=confirmed,
                     mode_was_sustained_while_requesting=receipt.get("mode_was_sustained_while_requesting"),
                     stable_feedback_s=engineering_bounds.get("stable_feedback_s")))
