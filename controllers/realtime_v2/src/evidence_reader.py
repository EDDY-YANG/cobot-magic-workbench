"""Versioned evidence reader with no controller or canonical runtime imports."""
import hashlib
import json
from pathlib import Path

from .packet_images import restore_packet_images

LEGACY_SCHEMA = 'v2-long-window-evidence-v1'
SCHEMA = 'v2-long-window-evidence-v2'
DESCRIPTOR_SCHEMA = 'v2-evidence-descriptor-v1'
CHUNK_SCHEMA = 'v2-evidence-chunk-v1'

def iter_evidence_records(path, *, require_complete=False):
    """Stream old JSON-list or new descriptor evidence, validating chunk hashes.

    Failed-run durable prefixes may be read with require_complete=False; their
    manifest still records complete=False. Relative paths cannot escape root.
    """
    path = Path(path)
    document = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(document, list):
        yield from document
        return
    if not isinstance(document, dict) or document.get('schema') != DESCRIPTOR_SCHEMA:
        raise ValueError('Unsupported evidence descriptor')
    root = path.parent.resolve()

    def safe(name):
        candidate = root / name
        if Path(name).is_absolute() or '..' in Path(name).parts or not candidate.resolve().is_relative_to(root):
            raise ValueError('Evidence path leaves descriptor directory')
        return candidate

    manifest = json.loads(safe(document['manifest']).read_text(encoding='utf-8'))
    if manifest.get('schema') not in (SCHEMA, LEGACY_SCHEMA) or (require_complete and not manifest.get('complete')):
        raise ValueError('Evidence manifest invalid or incomplete')
    stream = document['stream']
    state = manifest['streams'][stream]
    count = 0
    expected_sequence = 1
    for chunk in state['chunks']:
        file = safe(chunk['path'])
        digest = hashlib.sha256()
        with file.open('rb') as handle:
            while data := handle.read(1024 * 1024):
                digest.update(data)
        if digest.hexdigest() != chunk['sha256'] or file.stat().st_size != chunk['bytes']:
            raise ValueError('Evidence chunk checksum/length mismatch: ' + file.name)
        chunk_count = 0
        with file.open('r', encoding='utf-8') as handle:
            header = json.loads(next(handle))
            if header.get('schema') != CHUNK_SCHEMA or header.get('stream') != stream:
                raise ValueError('Evidence chunk header mismatch')
            for line in handle:
                row = json.loads(line)
                if row['sequence'] != expected_sequence:
                    if require_complete or row['sequence'] < expected_sequence:
                        raise ValueError('Evidence sequence gap/reordering')
                expected_sequence = row['sequence'] + 1
                chunk_count += 1
                count += 1
                record = row['record']
                if stream == 'packets' and manifest['schema'] == SCHEMA:
                    record = restore_packet_images(record, root)
                yield record
        if chunk_count != chunk['count'] or chunk_count != header['count']:
            raise ValueError('Evidence chunk record count mismatch')
    if count != state['written'] or count != document['count']:
        raise ValueError('Evidence descriptor/manifest total mismatch')
