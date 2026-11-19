"""Load the reviewed V3 class without opening CAN or constructing an SDK.

Only the fixed official 0.7.0a1 wheel and the reviewed firmware interval are
accepted. The caller owns socket acquisition, single-writer guards and actual
feedback; source compatibility does not establish calibration or motion safety.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
from pathlib import Path, PurePosixPath
import re
import sys
from collections.abc import Mapping


SDK_V3_VERSION = "0.7.0a1"
SDK_V3_INTERFACE = "C_PiperInterface_V3"
SDK_V3_TRANSPORT_ATTRIBUTE = "_arm_can"
SDK_V3_WHEEL_SHA256 = "599677152cc8bed8e0dd11cec89f46e0e7e95b2b2be002ffe74d1459e17e4d51"
_MANIFEST_PATH = Path(__file__).with_name(
    "2026-10-16-18-46-piper-sdk-0.7.0a1-reviewed-python-sources.json")
_MANIFEST_SHA256 = "0d48f48246616d5ab1ffe9239b09b4d6b34fa35e5598a3875c1d2438ff74cad5"
_V3_MODULE = "piper_sdk.interface.piper_interface_v3"
_V2_MODULE = "piper_sdk.interface.piper_interface_v2"
_ATTESTED_MODULES: dict[str, object] = {}


class SdkV3CompatibilityError(RuntimeError):
    """The installed package or declared firmware is outside this review."""


def _require_configuration(cfg: Mapping) -> None:
    if not isinstance(cfg, Mapping):
        raise SdkV3CompatibilityError("V3 configuration must be a mapping")
    if cfg.get("sdk_version") != SDK_V3_VERSION:
        raise SdkV3CompatibilityError("V3 requires explicit SDK pin 0.7.0a1")
    version = cfg.get("controller_firmware_version")
    match = re.fullmatch(r"S-V(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-(0|[1-9][0-9]*)",
                         version) if isinstance(version, str) else None
    if match is None or not (1, 8, 8) <= tuple(map(int, match.groups())) <= (1, 9, 1):
        raise SdkV3CompatibilityError(
            "Controller firmware must be explicitly within reviewed S-V1.8-8..S-V1.9-1")
    if "sdk_interface" in cfg and cfg["sdk_interface"] != SDK_V3_INTERFACE:
        raise SdkV3CompatibilityError("V3 SDK pin and sdk_interface do not match")
    if "protocol_version" in cfg and cfg["protocol_version"] != "v3":
        raise SdkV3CompatibilityError("V3 SDK pin and protocol_version do not match")


def _load_manifest() -> dict[str, str]:
    try:
        raw = _MANIFEST_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _MANIFEST_SHA256:
            raise SdkV3CompatibilityError("Reviewed V3 source manifest hash differs")
        value = json.loads(raw)
        if (value.get("schema") != "tiger-reviewed-piper-sdk-sources-v1"
                or value.get("sdk_version") != SDK_V3_VERSION
                or value.get("package") != "piper_sdk"
                or value.get("wheel_sha256") != SDK_V3_WHEEL_SHA256
                or value.get("python_source_count") != 195
                or not isinstance(value.get("files"), dict)
                or len(value["files"]) != 195):
            raise SdkV3CompatibilityError("Reviewed V3 source manifest contract differs")
        for name, digest in value["files"].items():
            path = PurePosixPath(name)
            if (not isinstance(name, str) or path.is_absolute()
                    or path.as_posix() != name or ".." in path.parts
                    or "\\" in name or not name.endswith(".py")
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                raise SdkV3CompatibilityError("Invalid entry in reviewed V3 manifest")
        return value["files"]
    except SdkV3CompatibilityError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SdkV3CompatibilityError("Cannot read reviewed V3 source manifest") from exc


def _verify_sources(root: Path, expected: Mapping[str, str]) -> None:
    try:
        actual = {}
        # A symlinked subdirectory must not hide extra source or escape the tree.
        for path in root.rglob("*"):
            if path.is_symlink():
                raise SdkV3CompatibilityError("Symlink inside reviewed V3 package: " + str(path))
            if path.is_file() and path.suffix.lower() == ".py":
                actual[path.relative_to(root).as_posix()] = path
        missing, extra = set(expected) - actual.keys(), actual.keys() - set(expected)
        if missing or extra:
            raise SdkV3CompatibilityError(
                "V3 package source set differs; missing=" + repr(sorted(missing))
                + ", extra=" + repr(sorted(extra)))
        for name, path in actual.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
                raise SdkV3CompatibilityError("V3 dependency source hash differs: " + name)
    except SdkV3CompatibilityError:
        raise
    except OSError as exc:
        raise SdkV3CompatibilityError("Cannot verify installed V3 sources") from exc


def _verify_loaded_modules(root: Path, expected: Mapping[str, str]) -> None:
    for name, module in tuple(sys.modules.items()):
        if name != "piper_sdk" and not name.startswith("piper_sdk."):
            continue
        origin = getattr(module, "__file__", None)
        try:
            path = Path(origin).resolve(strict=True) if isinstance(origin, str) else None
            relative = path.relative_to(root).as_posix() if path is not None else None
        except (OSError, ValueError) as exc:
            raise SdkV3CompatibilityError("Loaded SDK module comes from another source: " + name) from exc
        if relative not in expected:
            raise SdkV3CompatibilityError("Loaded SDK module is not in reviewed source set: " + name)


def _require_attested_cache() -> None:
    for name, module in tuple(sys.modules.items()):
        if name == "piper_sdk" or name.startswith("piper_sdk."):
            if _ATTESTED_MODULES.get(name) is not module or module is None:
                raise SdkV3CompatibilityError(
                    "Loaded SDK module was not attested by this loader: " + name
                    + "; use a new writer process after changing or preloading the SDK")


def _require_import_origin(root: Path) -> None:
    # Looking up a top-level package does not execute its __init__.py. Checking
    # only after import would execute a different sys.path package before refusal.
    spec = importlib.util.find_spec("piper_sdk")
    if spec is None or not isinstance(spec.origin, str):
        raise SdkV3CompatibilityError("Cannot resolve the reviewed V3 package import")
    origin = Path(spec.origin).resolve(strict=True)
    locations = spec.submodule_search_locations
    if (origin != root / "__init__.py" or locations is None
            or {Path(location).resolve(strict=True) for location in locations} != {root}):
        raise SdkV3CompatibilityError("SDK import resolution differs from the reviewed distribution")


def load_reviewed_v3_class(cfg: Mapping) -> type:
    """Return the verified official V3 class, never instantiate or connect it.

    `controller_firmware_version` is an explicit declared version. This loader
    neither queries hardware nor certifies that the declaration matches a device.
    Every dependency Python source is checked before importing the package.
    """
    _require_configuration(cfg)
    expected = _load_manifest()
    try:
        distribution = importlib.metadata.distribution("piper-sdk")
        if distribution.version != SDK_V3_VERSION:
            raise SdkV3CompatibilityError("Installed piper-sdk must be exactly 0.7.0a1")
        root = Path(distribution.locate_file("piper_sdk")).resolve(strict=True)
        if not root.is_dir():
            raise SdkV3CompatibilityError("Installed V3 package directory is missing")
        _verify_sources(root, expected)
        _require_attested_cache()
        _verify_loaded_modules(root, expected)
        _require_import_origin(root)
        package = importlib.import_module("piper_sdk")
        _verify_loaded_modules(root, expected)
        cls = getattr(package, SDK_V3_INTERFACE, None)
        if (not inspect.isclass(cls) or cls.__name__ != SDK_V3_INTERFACE
                or cls.__module__ != _V3_MODULE):
            raise SdkV3CompatibilityError("Installed SDK does not export the reviewed V3 class")
        defining_module = sys.modules.get(_V3_MODULE)
        base_module = sys.modules.get(_V2_MODULE)
        if (getattr(defining_module, SDK_V3_INTERFACE, None) is not cls
                or base_module is None
                or cls.__bases__ != (getattr(base_module, "C_PiperInterface_V2", None),)
                or Path(inspect.getfile(cls)).resolve(strict=True)
                != root / "interface" / "piper_interface_v3.py"):
            raise SdkV3CompatibilityError("V3 class definition or reviewed V2 inheritance differs")
        # Do not accept files replaced while import was in progress.
        _verify_sources(root, expected)
        _ATTESTED_MODULES.clear()
        _ATTESTED_MODULES.update(
            (name, module) for name, module in tuple(sys.modules.items())
            if name == "piper_sdk" or name.startswith("piper_sdk."))
        return cls
    except SdkV3CompatibilityError:
        raise
    except Exception as exc:
        raise SdkV3CompatibilityError("Cannot load the reviewed official V3 class") from exc
