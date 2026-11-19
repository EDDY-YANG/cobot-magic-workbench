"""Explicit model storage identity; planning never probes devices or model bytes.

Only a declared config chooses a new USB. Legacy callers retain the old fixed
identity. No mount discovery, guessed parents, or internal-disk fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import socket
import subprocess


CONFIG_NAME = '2026-10-06-14-05-config.json'


class ModelStorageError(RuntimeError):
    pass


def _require(ok, message):
    if not ok:
        raise ModelStorageError(message)


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()), 'Missing model storage field: ' + label)
    return value


def _absolute(value, label):
    value = _text(value, label)
    _require(value.startswith('/') and not value.startswith('//') and '\\' not in value
             and '\x00' not in value and all(part not in ('', '.', '..') for part in value.split('/')[1:]),
             'Model storage requires a canonical absolute Linux path: ' + label)
    return str(PurePosixPath(value))


def _inside(path, mount, label):
    _require(PurePosixPath(mount) in PurePosixPath(path).parents,
             'Model storage path must be strictly inside selected USB: ' + label)


@dataclass(frozen=True)
class ModelStorageProfile:
    hostname: str
    usb: str
    usb_uuid: str
    remote_bundle: str | None
    original_bundle: str
    rtc_server: str
    explicit: bool
    config_path: str | None

    def describe(self):
        return dict(schema='tiger-model-storage-profile-v1', hostname=self.hostname,
                    usb=self.usb, usb_uuid=self.usb_uuid, remote_bundle=self.remote_bundle,
                    model_sources={'original_bundle': self.original_bundle, 'rtc_server': self.rtc_server},
                    explicit_profile=self.explicit, config_path=self.config_path,
                    live_mount_verified=False, model_files_verified=False)


def read_profile(root, legacy):
    """Read bounded configuration only; no subprocess, model hashing or loading."""
    config = Path(root) / CONFIG_NAME
    if not config.exists():
        _require(not config.is_symlink(), 'Broken model config symlink: ' + str(config))
        return ModelStorageProfile(**legacy, remote_bundle=None, explicit=False, config_path=None)
    _require(config.is_file() and not config.is_symlink(), 'Model config must be an ordinary file: ' + str(config))
    _require(config.stat().st_size <= 64 * 1024, 'Model config exceeds 64 KiB')
    try:
        cfg = json.loads(config.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ModelStorageError('Invalid model storage config: ' + str(config)) from exc
    _require(isinstance(cfg, dict), 'Model storage config must be an object')
    host = _text(cfg.get('hostname'), 'hostname')
    usb = _absolute(cfg.get('usb'), 'usb')
    usb_uuid = _text(cfg.get('usb_uuid'), 'usb_uuid').upper()
    remote = _absolute(cfg.get('remote_bundle'), 'remote_bundle')
    _inside(remote, usb, 'remote_bundle')
    if 'model_sources' not in cfg:
        _require(host == legacy['hostname'] and usb == legacy['usb']
                 and usb_uuid == legacy['usb_uuid'],
                 'Selected USB requires explicit model_sources; no legacy fallback')
        sources = {key: legacy[key] for key in ('original_bundle', 'rtc_server')}
        explicit = False
    else:
        sources = cfg['model_sources']
        _require(isinstance(sources, dict), 'model_sources must be an object')
        _require(set(sources) == {'original_bundle', 'rtc_server'},
                 'model_sources requires exactly original_bundle and rtc_server')
        explicit = True
    original = _absolute(sources.get('original_bundle'), 'model_sources.original_bundle')
    server = _absolute(sources.get('rtc_server'), 'model_sources.rtc_server')
    _inside(original, usb, 'model_sources.original_bundle')
    _inside(server, usb, 'model_sources.rtc_server')
    return ModelStorageProfile(host, usb, usb_uuid, remote, original, server, explicit, str(config))


def contained_path(profile, path):
    """Reject symlinks and escapes before using an input, cache or output path."""
    mount = Path(profile.usb)
    target = Path(path)
    _require(target.is_absolute(), 'Model storage operation requires an absolute path')
    _require(mount.resolve() in target.resolve().parents,
             'Model path escaped selected USB: ' + str(target))
    for candidate in (target, *target.parents):
        _require(not candidate.is_symlink(), 'Model storage symbolic link refused: ' + str(candidate))
        if candidate == mount:
            break
    return target


def verify_profile(profile, path, root=None):
    _require(socket.gethostname() == profile.hostname, 'Wrong robot hostname')
    mount = Path(profile.usb)
    _require(mount.is_mount() and not mount.is_symlink(),
             'Actual robot USB required; no internal fallback')
    try:
        reply = json.loads(subprocess.check_output(
            ['findmnt', '-J', '-T', str(mount), '-o', 'TARGET,SOURCE,FSTYPE,UUID,OPTIONS'],
            text=True, timeout=5))
        mounts = reply['filesystems']
        _require(isinstance(mounts, list) and len(mounts) == 1, 'Ambiguous USB mount result')
        actual = mounts[0]
        _require(actual.get('target') == str(mount)
                 and str(actual.get('uuid', '')).upper() == profile.usb_uuid
                 and 'rw' in str(actual.get('options', '')).split(','),
                 'Robot USB target/UUID/writable mount mismatch')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ModelStorageError('Cannot verify selected USB mount: ' + str(mount)) from exc
    if root is not None and profile.remote_bundle is not None:
        _require(Path(root).resolve() == Path(profile.remote_bundle).resolve(),
                 'Runtime root differs from configured remote_bundle')
    for selected in (path, profile.original_bundle, profile.rtc_server):
        contained_path(profile, selected)
    return profile
