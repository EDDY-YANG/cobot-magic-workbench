"""Explicit offline detector alternatives, never a robot safety controller."""
import time
from .presence_detector import OpenCVPresenceDetector, DEFAULT_MODEL_ROOT

PROFILES = ('nanodet_palm', 'nanodet_handshape', 'yolox_handshape')

class PresencePipeline(OpenCVPresenceDetector):
    def __init__(self, profile='nanodet_palm', model_root=DEFAULT_MODEL_ROOT):
        if profile not in PROFILES:raise ValueError('Unknown frozen detector profile')
        super().__init__(model_root=model_root)
        self.profile=profile;self.yolox=None;self.handshape=None
        if profile=='yolox_handshape':
            from .presence_person_yolox import OpenCVYoloXPersonDetector
            self.yolox=OpenCVYoloXPersonDetector(model_root=model_root)
        if profile.endswith('handshape'):
            from .presence_handshape import OpenCVHandShapeVerifier
            self.handshape=OpenCVHandShapeVerifier(model_root=model_root)
        self.model_info.update(profile=profile,validation_scope='offline only; no protective certification',
                               handshape=self.handshape.model_info if self.handshape else None)
        if self.handshape:self.model_info['hand']='MediaPipe Palm proposals + 21-point hand-pose presence confirmation (official threshold .8)'
        if self.yolox:
            self.model_info['person']='YOLOX-S COCO person (active override)'
            self.model_info['person_override']=self.yolox.model_info
            self.model_info['unused_loaded_person_asset']='NanoDet is loaded by base construction but not executed in this profile'

    def _person(self,image):
        if not self.yolox:return super()._person(image)
        value=self.yolox.infer(image)
        if value['valid'] is not True:raise ValueError(value['error'])
        return value['persons']

    def infer(self,image):
        started=time.perf_counter();result=super().infer(image)
        result['profile']=self.profile
        if self.handshape and result['valid']:
            verified=self.handshape.verify(image,result['hands'])
            result['raw_palms']=verified['raw_palms']
            result['unconfirmed_hands']=verified['unconfirmed_hands']
            result['handshape_diagnostics']=verified['diagnostics']
            result['timing_ms']['handshape_ms']=verified['timing_ms']['total_ms']
            if verified['valid'] is not True:
                result.update(valid=False,error='hand-shape verification: '+str(verified['error']),present=None)
            else:
                result['hands']=verified['hands']
                result['present']=bool(result['persons'] or result['hands'])
            result['timing_ms']['total_ms']=(time.perf_counter()-started)*1000
        return result
