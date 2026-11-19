"""Whitelisted, USB-bound offline person/hand replay. No control interface."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

class PresencePresentation:
    def __init__(self,config):
        from .semantic_workspace import verify_output_volume
        self.root=verify_output_volume(config['output_root'],config['output_volume_serial']).resolve()
        self.serial=config['output_volume_serial'];path=Path(config['manifest_path']).resolve()
        self.asset_roots=tuple(dict.fromkeys([self.root]+[
            verify_output_volume(value,self.serial).resolve() for value in config.get('asset_roots',[])]))
        if not path.is_relative_to(self.root):raise ValueError('Presence manifest outside selected USB output')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=config['manifest_sha256']:raise ValueError('Presence manifest SHA differs')
        self.value=json.loads(raw.decode('utf-8'))
        if self.value.get('schema')!='tiger-presence-presentation-v1' or self.value.get('source')!='offline_replay':
            raise ValueError('Presence requires an explicit offline manifest')
        if self.value.get('motion_authority') is not False or type(self.value.get('commands_sent')) is not int or self.value['commands_sent']!=0:
            raise ValueError('Presence replay must have zero control authority')
        self._clips={};self._images={};self._verified={};self._lock=threading.Lock()
        for clip in self.value['clips']:
            key=clip['id'];target=Path(clip['video']['path']).resolve();digest=clip['video']['sha256']
            if not isinstance(key,str) or not key or key in self._clips or not any(target.is_relative_to(base) for base in self.asset_roots) or target.suffix!='.mp4' or len(digest)!=64:
                raise ValueError('Invalid reviewed presence clip')
            self._clips[key]=(target,digest)
        for item in self.value.get('images',[]):
            key=item['id'];target=Path(item['image']['path']).resolve();digest=item['image']['sha256']
            if not isinstance(key,str) or not key or key in self._images or not any(target.is_relative_to(base) for base in self.asset_roots) or target.suffix not in ('.jpg','.png') or len(digest)!=64:
                raise ValueError('Invalid reviewed presence image')
            self._images[key]=(target,digest)

    def snapshot(self):
        from .semantic_workspace import verify_output_volume
        verify_output_volume(self.root,self.serial)
        value=deepcopy(self.value)
        for clip in value['clips']:
            clip['video'].pop('path',None)
        for item in value.get('images',[]):item['image'].pop('path',None)
        return value

    def video(self,clip_id):
        if clip_id not in self._clips:raise ValueError('Unknown reviewed presence clip')
        return self._checked_asset(*self._clips[clip_id])

    def image(self,image_id):
        if image_id not in self._images:raise ValueError('Unknown reviewed presence image')
        path=self._checked_asset(*self._images[image_id])
        return path.read_bytes(),'image/png' if path.suffix=='.png' else 'image/jpeg'

    def _checked_asset(self,path,expected):
        from .semantic_workspace import verify_output_volume
        verify_output_volume(self.root,self.serial)
        if not any(path.resolve().is_relative_to(base) for base in self.asset_roots):raise ValueError('Presence video moved outside selected USB output')
        stat=path.stat();signature=(stat.st_size,stat.st_mtime_ns)
        with self._lock:
            if self._verified.get(path)!=signature:
                with path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
                if digest!=expected:raise ValueError('Presence video SHA differs')
                self._verified[path]=signature
        return path
