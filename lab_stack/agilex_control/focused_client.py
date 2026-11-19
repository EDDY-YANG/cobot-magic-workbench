"""Catalog-bound, offline event/recent/current phase client. No hardware IO."""
from copy import deepcopy

from .semantic_observer import StartLuxHTTPClient,SemanticServiceError,validate_snapshot,_request_input
from .focused_semantic import payload_for
from .semantic_tail_review import tail_motion_assessment

PROFILE='focused_dual_history_v2'

class FocusedReplayClient(StartLuxHTTPClient):
    def __init__(self,catalog,**kwargs):
        super().__init__(input_profile='temporal_phase_mosaic_trim_v1',**kwargs)
        self.profile=PROFILE
        self.catalog=catalog

    def decide(self,snapshot):
        try:
            snapshot=validate_snapshot(snapshot)
            if snapshot['source']!='replay':raise ValueError('Focused phase profile is reviewed replay only')
            binding=snapshot['stamps'].get('focused_replay')
            if not isinstance(binding,dict) or set(binding)!={'episode_id','index'} or type(binding['index']) is not int:
                raise ValueError('Missing reviewed catalog sample binding')
            expected=self.catalog.temporal_observation(binding['episode_id'],binding['index'])
            stamps=deepcopy(snapshot['stamps']);stamps.pop('focused_replay',None)
            if (stamps!=expected['stamps'] or snapshot['frame_id']!=expected['frame_id'] or snapshot['task_id']!=expected['task_id'] or
                snapshot['state']!=expected['state'] or snapshot['images']!=expected['images'] or
                snapshot['image_roles']!=expected['image_roles']):
                raise ValueError('Focused sample differs from the frozen catalog observation')
            payload,evidence=payload_for(self.catalog,snapshot,binding['episode_id'],'dual_history_v2')
            self.catalog._guard()
            metadata=_request_input(payload,self.profile)
            metadata['focused_evidence']=evidence
        except (KeyError,ValueError,TypeError,OSError) as exc:
            raise SemanticServiceError('invalid_input','Focused causal input rejected: '+str(exc)[:300]) from None
        result=self._decide_payload(payload)
        result.update(input_profile=self.profile,request_input=metadata,not_requested=['holding','placement'])
        result['phase_assessment']=tail_motion_assessment(result['answers']['phase'],snapshot)
        return result
