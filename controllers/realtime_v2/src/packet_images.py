"""Lossless packet image attachments for V2 evidence, independent of hardware."""

import base64
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re


CAMERAS = ('front', 'right')
# Same per-camera raw JPEG limit as the Hub which produces the observations.
MAX_JPEG_BYTES = 3 * 1024 * 1024
REFERENCE_SCHEMA = 'v2-packet-jpeg-ref-v1'
OBSERVATION_SCHEMA = 'tiger-fp32-shadow-observation-v1'


@dataclass(frozen=True)
class PacketImages:
    snapshot: dict
    filename: str
    data: bytes


def prepare_snapshot(snapshot):
    """Validate and detach exact model-input bytes. Production runs this on its model worker."""
    if not isinstance(snapshot, dict):
        return snapshot
    frames = snapshot.get('frames')
    if frames is None and snapshot.get('schema') != OBSERVATION_SCHEMA:
        return snapshot
    if not isinstance(frames, dict) or set(frames) != set(CAMERAS):
        raise ValueError('V2 packet images: expected front and right frames')

    parts = []
    compact = {}
    offset = 0
    for role in CAMERAS:
        original = frames[role]
        if not isinstance(original, dict) or 'jpeg_ref' in original:
            raise ValueError(f'V2 packet images: invalid or pre-referenced {role} frame')
        frame = dict(original)
        encoded = frame.pop('jpeg_base64', None)
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 4 * ((MAX_JPEG_BYTES + 2) // 3):
            raise ValueError(f'V2 packet images: invalid {role} encoded length')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError(f'V2 packet images: invalid {role} base64') from exc
        if not (raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9')
                and len(raw) <= MAX_JPEG_BYTES and sha256(raw).hexdigest() == frame.get('sha256')):
            raise ValueError(f'V2 packet images: {role} JPEG content or SHA-256 mismatch')
        if base64.b64encode(raw).decode('ascii') != encoded:
            raise ValueError(f'V2 packet images: noncanonical {role} base64')
        frame['jpeg_ref'] = {'schema': REFERENCE_SCHEMA, 'offset': offset, 'bytes': len(raw)}
        compact[role] = frame
        parts.append(raw)
        offset += len(raw)
    data = b''.join(parts)
    filename = 'input-' + sha256(data).hexdigest() + '.bin'
    for frame in compact.values():
        frame['jpeg_ref']['file'] = filename
    return PacketImages({**snapshot, 'frames': compact}, filename, data)


def restore_packet_images(row, root):
    """Rebuild the historical inline view only after checking the file and both frames."""
    snapshot = row.get('captured_snapshot')
    if not isinstance(snapshot, dict):
        return row
    frames = snapshot.get('frames')
    if frames is None and snapshot.get('schema') != OBSERVATION_SCHEMA:
        return row
    if not isinstance(frames, dict) or set(frames) != set(CAMERAS):
        raise ValueError('V2 packet evidence missing required image references')
    refs = []
    for role in CAMERAS:
        frame = frames[role]
        ref = frame.get('jpeg_ref') if isinstance(frame, dict) else None
        if (not isinstance(ref, dict) or ref.get('schema') != REFERENCE_SCHEMA
                or 'jpeg_base64' in frame):
            raise ValueError(f'V2 packet evidence invalid image reference: {role}')
        refs.append(ref)
    name = refs[0].get('file')
    if not isinstance(name, str) or not re.fullmatch(r'input-[0-9a-f]{64}\.bin', name):
        raise ValueError('V2 packet evidence invalid image filename')
    offset = 0
    for ref in refs:
        length = ref.get('bytes')
        if (ref.get('file') != name or type(ref.get('offset')) is not int or ref['offset'] != offset
                or type(length) is not int or not 0 < length <= MAX_JPEG_BYTES):
            raise ValueError('V2 packet evidence invalid image bounds')
        offset += length
    path = Path(root) / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('V2 packet evidence missing or linked image file: ' + name)
    with path.open('rb') as handle:
        data = handle.read(offset + 1)
    if len(data) != offset or name != 'input-' + sha256(data).hexdigest() + '.bin':
        raise ValueError('V2 packet evidence image file checksum/length mismatch: ' + name)
    restored = {}
    for role, ref in zip(CAMERAS, refs):
        raw = data[ref['offset']:ref['offset'] + ref['bytes']]
        frame = dict(frames[role])
        if sha256(raw).hexdigest() != frame.get('sha256'):
            raise ValueError(f'V2 packet evidence {role} frame SHA-256 mismatch')
        del frame['jpeg_ref']
        frame['jpeg_base64'] = base64.b64encode(raw).decode('ascii')
        restored[role] = frame
    return {**row, 'captured_snapshot': {**snapshot, 'frames': restored}}
