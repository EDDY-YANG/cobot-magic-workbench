"""Read-only USB discovery and frozen per-session storage configuration.

Device names/labels help the operator; physical USB ancestry, a mounted writable
filesystem and UUID establish identity. No mount, format, mkdir or write occurs
here. Fixtures passed to inventory_from_snapshots are explicitly offline data.
"""
import copy
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess

SCHEMA = 'tiger-usb-inventory-v1'
SELECTION_SCHEMA = 'tiger-usb-selection-v1'
_SYSTEM_MOUNTS = {'/', '/boot', '/boot/efi', '/home', '/usr', '/var'}
_USB_PART = re.compile(r'usb[0-9]+$')


def _within(path, parent):
    """Path containment compatible with the robot's system Python 3.8."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _flatten(items, ancestors=()):
    for entry in items:
        yield entry, ancestors
        yield from _flatten(entry.get('children') or [], (*ancestors, entry))


def _mounts(value):
    result = value.get('mountpoints', value.get('mountpoint')) or []
    return [result] if isinstance(result, str) else [x for x in result if x]


def _physical_usb_path(sysfs_path):
    parts = PurePosixPath(str(sysfs_path)).parts
    for index, part in enumerate(parts):
        if _USB_PART.fullmatch(part) and index + 1 < len(parts):
            # USB bus plus physical device, not its partition or interface.
            return str(PurePosixPath(*parts[:index + 2]))
    return None


def inventory_from_snapshots(blocks, mounts, *, hostname, sysfs_paths,
                             free_bytes=None, writable=None, canonical_paths=None):
    """Pure parser for actual command snapshots; never substitutes sensor data."""
    free_bytes, writable = free_bytes or {}, writable or {}
    canonical_paths = canonical_paths or {}
    entries = list(_flatten(blocks.get('blockdevices') or []))
    mounted = list(_flatten(mounts.get('filesystems') or []))
    candidates = []
    for device, ancestors in entries:
        ancestry = (*ancestors, device)
        usb_transport = any(str(x.get('tran', '')).lower() == 'usb' for x in ancestry)
        name = str(device.get('name') or '')
        physical = _physical_usb_path(sysfs_paths.get(name, ''))
        if not usb_transport and not physical:
            continue  # Internal disks are never selectable.
        root_disk = next((x for x in ancestry if x.get('type') == 'disk'), device)
        disk_names = {str(x.get('name')) for x, parents in entries
                      if x is root_disk or any(y is root_disk for y in parents)}
        system_disk = any(m.get('target') in _SYSTEM_MOUNTS and
                          str(m.get('source', '')).split('[')[0] in disk_names
                          for m, _ in mounted)
        current_mounts = [(m, parents) for m, parents in mounted
                          if str(m.get('source', '')).split('[')[0] == name]
        if device.get('type') not in ('part', 'disk') or (device.get('children') and not current_mounts):
            continue
        if not current_mounts:
            current_mounts = [({}, ())]
        for mount, _ in current_mounts:
            target = str(mount.get('target') or '')
            uuid = str(mount.get('uuid') or device.get('uuid') or '').upper()
            source = str(mount.get('source') or name)
            label = str(device.get('label') or '')
            model = str(root_disk.get('model') or '').strip()
            options = str(mount.get('options') or '').split(',')
            reasons = []
            if not usb_transport or not physical:
                reasons.append('USB physical ancestry could not be verified')
            if system_disk:
                reasons.append('System filesystem uses this physical disk')
            if not target or target in _SYSTEM_MOUNTS:
                reasons.append('USB is not mounted at a separate data mountpoint')
            if target and (not PurePosixPath(target).is_absolute() or
                           canonical_paths.get(target, target) != target):
                reasons.append('Mountpoint must be a canonical absolute path')
            if 'rw' not in options or device.get('ro') in (True, 1, '1'):
                reasons.append('Filesystem is not mounted read-write')
            if writable.get(target) is not True:
                reasons.append('Mountpoint is not writable by the runtime user')
            if not uuid:
                reasons.append('Filesystem UUID is unavailable')
            major_minor = str(mount.get('maj:min') or device.get('maj:min') or '')
            if not re.fullmatch(r'[0-9]+:[0-9]+', major_minor):
                reasons.append('Block-device identity is unavailable')
            candidates.append({'uuid': uuid, 'mountpoint': target, 'source': source,
                'device': name, 'major_minor': major_minor,
                'physical_usb_path': physical, 'filesystem': mount.get('fstype') or device.get('fstype'),
                'label': label, 'model': model, 'display_name': label or model or uuid or name,
                'mounted': bool(target),
                'size_bytes': int(device.get('size') or 0),
                'available_bytes': free_bytes.get(target), 'eligible': not reasons,
                'reasons': reasons})
    # Cloned UUIDs and bind mounts are ambiguous even if a particular row was clicked.
    counts = {}
    for candidate in candidates:
        if candidate['uuid']:
            counts[candidate['uuid']] = counts.get(candidate['uuid'], 0) + 1
    for candidate in candidates:
        if counts.get(candidate['uuid'], 0) > 1:
            candidate['eligible'] = False
            candidate['reasons'].append('Duplicate UUID or multiple mounts; identity is ambiguous')
    return {'schema': SCHEMA, 'hostname': hostname,
            'checked_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
            'candidates': candidates, 'writes_performed': False}


def discover_usb_storage(expected_hostname=None):
    """Read kernel/storage metadata only; does not inspect cameras or CAN."""
    hostname = socket.gethostname()
    if expected_hostname and hostname != expected_hostname:
        raise RuntimeError('Wrong host; refusing USB discovery')
    blocks = json.loads(subprocess.check_output(['lsblk', '-b', '-p', '-J', '-o',
        'NAME,KNAME,TYPE,TRAN,RM,SIZE,MODEL,LABEL,FSTYPE,UUID,MOUNTPOINT,RO,MAJ:MIN'],
        text=True, timeout=5))
    mounts = json.loads(subprocess.check_output(['findmnt', '-J', '-o',
        'TARGET,SOURCE,FSTYPE,UUID,OPTIONS,MAJ:MIN'], text=True, timeout=5))
    paths, available, writable, canonical = {}, {}, {}, {}
    for device, _ in _flatten(blocks.get('blockdevices') or []):
        name = str(device.get('name') or '')
        kname = str(device.get('kname') or Path(name).name)
        paths[name] = str((Path('/sys/class/block') / Path(kname).name).resolve())
    for mount, _ in _flatten(mounts.get('filesystems') or []):
        target = str(mount.get('target') or '')
        try:
            canonical[target] = str(Path(target).resolve(strict=True))
            writable[target] = os.access(target, os.W_OK | os.X_OK)
            stat = os.statvfs(target)
            available[target] = stat.f_bavail * stat.f_frsize
        except (OSError, ValueError):
            writable[target] = False
    return inventory_from_snapshots(blocks, mounts, hostname=hostname,
        sysfs_paths=paths, free_bytes=available, writable=writable, canonical_paths=canonical)


def choose_storage(inventory, selected_uuid=None, selected_mountpoint=None):
    if inventory.get('schema') != SCHEMA:
        raise ValueError('Invalid USB inventory')
    candidates = [x for x in inventory.get('candidates', []) if x.get('eligible') is True]
    if selected_uuid is not None:
        candidates = [x for x in candidates if x['uuid'] == str(selected_uuid).upper()]
        if selected_mountpoint is not None:
            candidates = [x for x in candidates if x['mountpoint'] == selected_mountpoint]
    elif selected_mountpoint is not None:
        raise ValueError('USB selection requires UUID and mountpoint together')
    if len(candidates) != 1:
        raise RuntimeError('No eligible mounted writable USB' if not candidates else
                           'Multiple eligible USB devices; explicitly choose one')
    return copy.deepcopy(candidates[0])


def config_for_storage(cfg, candidate):
    """Freeze one verified selection; never edits the PC base configuration."""
    if candidate.get('eligible') is not True:
        raise ValueError('Cannot select an ineligible USB')
    old, target = PurePosixPath(cfg['usb']), PurePosixPath(candidate['mountpoint'])
    if not target.is_absolute() or str(target) in _SYSTEM_MOUNTS or '..' in target.parts:
        raise ValueError('Invalid USB mountpoint')
    required = ('uuid', 'source', 'device', 'major_minor', 'physical_usb_path')
    if any(not isinstance(candidate.get(key), str) or not candidate[key] for key in required):
        raise ValueError('Incomplete USB identity')
    def rebase(value):
        if isinstance(value, dict):
            return {key: rebase(item) for key, item in value.items() if key != 'usb_selection'}
        if isinstance(value, list):
            return [rebase(item) for item in value]
        if isinstance(value, str) and value.startswith('/'):
            path = PurePosixPath(value)
            try:
                return str(target / path.relative_to(old))
            except ValueError:
                pass
        return copy.deepcopy(value)
    result = rebase(cfg)
    result['usb'], result['usb_uuid'] = str(target), candidate['uuid'].upper()
    if not _within(PurePosixPath(result['remote_bundle']), target):
        raise ValueError('Runtime source bundle must remain on selected USB')
    result['usb_selection'] = {'schema': SELECTION_SCHEMA,
        **{key: copy.deepcopy(candidate[key]) for key in required},
        'mountpoint': str(target), 'uuid': candidate['uuid'].upper(),
        'label': candidate.get('label', ''), 'display_name': candidate.get('display_name', ''),
        'filesystem': candidate.get('filesystem')}
    return result


def normalized_session_config(cfg):
    """Compare policy/device configuration independently of a USB binding.

    Only the top-level storage identity fields are excluded. Every other key,
    including camera IDs, modes, limits and model contracts, remains exact.
    Absolute paths beneath that configuration's USB become logical USB paths.
    """
    if not isinstance(cfg, dict) or not isinstance(cfg.get('usb'), str):
        raise ValueError('Invalid session configuration')
    prefix = PurePosixPath(cfg['usb'])
    if not prefix.is_absolute() or str(prefix) in _SYSTEM_MOUNTS or '..' in prefix.parts:
        raise ValueError('Invalid configuration USB prefix')
    ignored = {'usb', 'usb_uuid', 'remote_bundle', 'usb_selection'}
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str) and value.startswith('/'):
            try:
                suffix = PurePosixPath(value).relative_to(prefix)
            except ValueError:
                pass
            else:
                return '__SELECTED_USB__/' + suffix.as_posix()
        return copy.deepcopy(value)
    return {key: normalize(value) for key, value in cfg.items() if key not in ignored}


def session_config_compatible(previous, current):
    return normalized_session_config(previous) == normalized_session_config(current)


def validate_session_config_refresh(previous, current, actual_sources, expected_sources,
                                    config_name='2026-10-06-14-05-config.json'):
    """Allow only a storage rebinding after every other delivered SHA matches."""
    expected = {name: digest for name, digest in expected_sources.items() if name != config_name}
    if config_name not in expected_sources or actual_sources != expected:
        raise RuntimeError('Non-configuration source mismatch; explicit sync required')
    if not session_config_compatible(previous, current):
        raise RuntimeError('Non-storage configuration differs; explicit sync required')


def verify_frozen_identity(cfg, mount):
    """Additional hot-path checks after findmnt; no scanning or disk writes."""
    selected = cfg.get('usb_selection')
    if selected is None:
        return  # Historical explicit-UUID config retains its existing guard.
    if selected.get('schema') != SELECTION_SCHEMA:
        raise RuntimeError('Invalid frozen USB identity')
    expected = {'target': selected.get('mountpoint'), 'source': selected.get('source'),
                'maj:min': selected.get('major_minor')}
    if (cfg.get('usb') != selected.get('mountpoint') or
            str(cfg.get('usb_uuid', '')).upper() != str(selected.get('uuid', '')).upper() or
            any(str(mount.get(key, '')) != str(value) for key, value in expected.items())):
        raise RuntimeError('Selected USB changed; stop instead of switching storage')
    sysfs = str((Path('/sys/class/block') / Path(selected['device']).name).resolve())
    if _physical_usb_path(sysfs) != selected.get('physical_usb_path'):
        raise RuntimeError('Selected physical USB device changed')
    root = Path(cfg['usb'])
    if str(root.resolve()) != str(root) or not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeError('Selected USB path is redirected or not writable')


def require_usb_path(path, cfg, root=None):
    """Reject symlink/path escapes before any session directory is created."""
    root = Path(cfg['usb']) if root is None else Path(root)
    target = Path(path)
    if not target.is_absolute() or not _within(target.resolve(), root.resolve()):
        raise RuntimeError('Recording/evidence path must remain within selected USB')
    if target.resolve() == root.resolve():
        raise RuntimeError('Recording/evidence path cannot be the mountpoint itself')
    return target


def open_usb_directory(path, cfg, *, create=False, exclusive_final=False):
    """Linux directory descriptor anchored on the frozen filesystem.

    Use only after verify_usb. This closes the unmount/replacement race between
    checking a pathname and creating files under that pathname.
    """
    root = Path(cfg['usb'])
    target = Path(path)
    if target != root:
        target = require_usb_path(path, cfg, root)
    selected = cfg.get('usb_selection')
    if not selected:
        raise ValueError('Anchored directory requires a frozen USB selection')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        device = os.fstat(descriptor).st_dev
        if f'{os.major(device)}:{os.minor(device)}' != selected['major_minor']:
            raise RuntimeError('Selected USB detached/replaced before directory access')
        parts = target.relative_to(root).parts
        for index, part in enumerate(parts):
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    if exclusive_final and index == len(parts) - 1:
                        raise
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            if os.fstat(following).st_dev != device:
                os.close(following)
                raise RuntimeError('Nested mount crossed outside the selected USB filesystem')
            os.close(descriptor)
            descriptor = following
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
