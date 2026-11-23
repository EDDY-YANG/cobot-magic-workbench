"""Inspect PiPER's installed pair-home method without opening a CAN device."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
import json
from pathlib import Path

EXPECTED = {
    0: (0x191, bytes.fromhex("0100000000000000")),
    1: (0x191, bytes.fromhex("0101010000000000")),
    2: (0x191, bytes.fromhex("0100010000000000")),
}


class ProbeError(RuntimeError):
    pass


class _Message:
    def __init__(self):
        self.arbitration_id = None
        self.data = []


class _Capture:
    def __init__(self):
        self.frames = []

    def SendCanMessage(self, can_id, data):
        self.frames.append((can_id, bytes(data)))


def probe_source(source: str) -> dict:
    """Execute only the installed method AST against a memory-only transport.

    The SDK module, constructor, ConnectPort and original transport are never
    imported or executed. Restricted builtins also reject imports/file access.
    """
    tree = ast.parse(source)
    classes = [n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "C_PiperInterface_V2"]
    if len(classes) != 1:
        raise ProbeError("Expected exactly one C_PiperInterface_V2 class")
    methods = [n for n in classes[0].body
               if isinstance(n, ast.FunctionDef) and n.name == "ReqMasterArmMoveToHome"]
    if len(methods) != 1:
        raise ProbeError("Installed source has no unique pair-home method")
    method = copy.deepcopy(methods[0])
    if method.decorator_list or method.args.defaults or method.args.kw_defaults:
        raise ProbeError("Unexpected decorators or defaults; review changed SDK")
    for arg in (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs):
        arg.annotation = None
    method.returns = None
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(
        name="C_PiperInterface_V2", bases=[], keywords=[], body=[method],
        decorator_list=[])], type_ignores=[]))
    namespace = {
        "__name__": "piper_pair_home_offline_probe",
        "__builtins__": {"__build_class__": builtins.__build_class__},
        "Message": _Message,
    }
    try:
        exec(compile(isolated, "<installed-piper-pair-home-method>", "exec"), namespace)
        instance = namespace["C_PiperInterface_V2"]()
        capture = _Capture()
        instance._C_PiperInterface_V2__arm_can = capture
        cases = []
        for mode, expected in EXPECTED.items():
            begin = len(capture.frames)
            instance.ReqMasterArmMoveToHome(mode)
            emitted = capture.frames[begin:]
            if emitted != [expected]:
                raise ProbeError(f"Mode {mode} frame differs from documented contract")
            cases.append({"mode": mode, "can_id": hex(expected[0]),
                          "data_hex": expected[1].hex(), "passed": True})
    except ProbeError:
        raise
    except Exception as exc:
        raise ProbeError(f"Offline method execution rejected: {type(exc).__name__}") from exc
    return {
        "status": "OFFLINE_PACKET_CONTRACT_PASSED",
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "cases": cases,
        "sdk_constructor_executed": False,
        "physical_motion_tested": False,
        "arbitrary_joint_target_support_verified": False,
        "limitations": "Tests packet construction only; not firmware compatibility, "
                      "live CAN delivery, actual motion, holding or safe handover.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-interface-file", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe_source(args.sdk_interface_file.read_text(encoding="utf-8")),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
