"""Read-only archived offline model series and exact, USB-bound MP4 assets."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading


class SemanticPresentation:
    def __init__(self, path, expected_sha256, output_root, serial):
        from .semantic_workspace import verify_output_volume
        self.root = verify_output_volume(output_root, serial).resolve()
        self.serial = serial
        self.path = Path(path).resolve()
        if not self.path.is_relative_to(self.root):
            raise ValueError('Presentation must stay in the selected runtime USB directory')
        raw = self.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('Presentation manifest SHA-256 differs')
        self.value = json.loads(raw.decode('utf-8'))
        if self.value.get('schema') != 'tiger-semantic-presentation-v1' or self.value.get('source') != 'historical_replay':
            raise ValueError('Only the frozen offline presentation is accepted')
        self._videos, self._verified, self._lock = {}, {}, threading.Lock()
        for episode in self.value['episodes']:
            assets={None:episode['video'],**episode.get('videos',{})}
            for profile,video in assets.items():
                target=Path(video['path']).resolve()
                if not target.is_relative_to(self.root) or target.suffix!='.mp4' or len(video['sha256'])!=64:
                    raise ValueError('Video must be an exact MP4 asset in the selected USB directory')
                self._videos[(episode['episode_id'],profile)]=(target,video['sha256'])

    def snapshot(self):
        from .semantic_workspace import verify_output_volume
        verify_output_volume(self.root, self.serial)
        value = deepcopy(self.value)
        for episode in value['episodes']:
            episode['video'].pop('path', None)
            for asset in episode.get('videos',{}).values():
                asset.pop('path',None)
        return value

    def video(self, episode_id, profile=None):
        from .semantic_workspace import verify_output_volume
        verify_output_volume(self.root, self.serial)
        key=(episode_id,profile)
        if key not in self._videos:
            raise ValueError('Video is outside the two reviewed recordings')
        path, expected = self._videos[key]
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if self._verified.get(path) != signature:
                with path.open('rb') as f:
                    actual = hashlib.file_digest(f, 'sha256').hexdigest()
                if actual != expected:
                    raise ValueError('Offline MP4 SHA-256 differs')
                self._verified[path] = signature
        return path


def byte_range(header, size):
    """Single RFC-style byte range for seekable browser playback; no multipart."""
    if header is None:
        return 0, size - 1, False
    if not header.startswith('bytes=') or ',' in header:
        raise ValueError('Invalid video byte range')
    parts = header[6:].split('-')
    if len(parts) != 2 or not any(parts) or any(p and not p.isdigit() for p in parts):
        raise ValueError('Invalid video byte range')
    first, last = parts
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError('Invalid video suffix range')
        return max(0, size-suffix), size-1, True
    first = int(first)
    last = min(size-1, int(last)) if last else size-1
    if first >= size or first > last:
        raise ValueError('Video range outside asset')
    return first, last, True
