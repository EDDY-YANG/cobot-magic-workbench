# PC PowerShell，项目根目录：只运行离线协议测试，不连接机械臂。
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_13_17_57_gripper_ready.py' -v
"""Pure decoding of an actual eight-byte V2/V3 0x2A8 reply.

Status flags are controller reports, not independent temperature/current tests.
Missing bytes are unknown: callers must retain the ValueError, never assume
healthy/enabled. V3 byte 7 identifies width/angle; only width is a linear
aperture. No hardware or optional dependency is imported here.
"""
import struct


FAULT_BITS = (
    (0, "voltage_too_low", "控制器报告电压过低"),
    (1, "motor_overheating", "控制器报告夹爪电机过温"),
    (2, "driver_overcurrent", "控制器报告驱动器过流"),
    (3, "driver_overheating", "控制器报告驱动器过温"),
    (4, "sensor_status", "控制器报告传感器异常"),
    (5, "driver_error_status", "控制器报告驱动器错误"),
)


def decode_gripper_feedback(payload: bytes | str) -> dict:
    """Retain signed raw value and mode; do not invent a linear-angle mapping.

    Width mode (0) is also the legacy V2 default: raw units are 0.001 mm.
    Angle mode (1) uses 0.001 degree. Unknown modes retain their raw value,
    status and byte code, with both physical-position interpretations absent.
    Controller health is independent of whether the mode is usable by a
    linear-position consumer; those consumers must require width explicitly.
    """
    if not isinstance(payload, (bytes, str)):
        raise ValueError("Actual gripper 0x2A8 payload must be bytes or hexadecimal text")
    try:
        raw = bytes.fromhex(payload) if isinstance(payload, str) else payload
    except ValueError as exc:
        raise ValueError("Malformed actual gripper 0x2A8 hexadecimal payload") from exc
    if len(raw) != 8:
        raise ValueError("Actual gripper 0x2A8 must contain eight bytes; status unknown")
    position, effort, status, mode_code = struct.unpack(">ihBB", raw)
    mode = {0: "width", 1: "angle"}.get(mode_code, "unknown")
    faults = [(name, label) for bit, name, label in FAULT_BITS if status & (1 << bit)]
    return {"raw_payload_hex": raw.hex(), "status_code": status,
            "status_hex": f"0x{status:02X}",
            "position_raw": position, "mode_code": mode_code, "mode": mode,
            "position_unit": {0: "0.001_mm", 1: "0.001_deg"}.get(mode_code),
            "linear_position_available": mode_code == 0,
            "aperture_m": position / 1_000_000 if mode_code == 0 else None,
            "angle_deg": position / 1000 if mode_code == 1 else None,
            "effort_Nm": effort / 1000, "fault_flags": [name for name, _ in faults],
            "fault_labels": [label for _, label in faults],
            "enabled": bool(status & 0x40), "homed": bool(status & 0x80),
            "healthy": not faults, "reserved_byte": mode_code,
            "status_source": "controller_reported_0x2A8",
            "temperature_measurement_available": False}
