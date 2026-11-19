"""Causal input experiments, never episode schedules or stage answers."""
import base64
from copy import deepcopy
import hashlib
import io
import math

from agilex_control.temporal_semantic import phase_payload, mosaic

VARIANTS=('definition_v2','aperture_events_v2','event_anchor_v2','dual_history_v2','event_anchor_compact_v2','wrist_detail_v2')
QUESTION={'phase':{'type':'choice','instructions':
    'Judge the CURRENT RIGHT-arm phase from observed cube/fingers/table/bucket relations and changes. '
    'Top images are PAST, bottom CURRENT. Aperture opening is not proof of release. '
    'Do not infer an expected task sequence. Distinguish lifting a held cube from closing on a table-supported cube.',
    'criteria':{
    'approaching':'Empty fingers approach/align with a red cube still on the table, before contact/closure.',
    'grasping':'Fingers contact/close on the cube WHILE it is still table-supported. Ends when cube lifts away.',
    'transporting':'Cube is OFF the table and moves WITH fingers, being lifted/carried toward bucket. Closed fingers holding a lifted cube are NOT still grasping.',
    'placing':'Held cube reaches the bucket opening and is being lowered/inserted/released there. Ends when cube separates and hand moves away.',
    'returning':'AFTER visible separation/release, EMPTY fingers/arm move away or reset wrist/joints. Cube remains behind. Empty fingers may close again during this retreat.',
    'unknown':'Stationary before approach or after return; missing/ambiguous/contradictory object-motion evidence.'}}}

def valid_gripper_points(history,now,domain,window_s=8):
    selected=[];seen=set();last=None
    for row in history:
        stamp=row.get('sample_monotonic_ns')
        if type(stamp) is not int or not max(0,now-int(window_s*1e9))<=stamp<=now or row.get('clock_domain')!=domain:
            continue
        arm=row.get('right',{});timing=arm.get('source_timing',{}).get('gripper',{})
        source,available=timing.get('source_monotonic_ns_estimate'),timing.get('available_monotonic_ns')
        identity=timing.get('sample_id');value=arm.get('grip_m');age=arm.get('age_gripper_s')
        if (arm.get('feedback_fresh') is not True or isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value)
            or not isinstance(age,(float,int)) or isinstance(age,bool) or not math.isfinite(age) or not 0<=age<=.2
            or timing.get('clock_domain')!=domain or type(source) is not int or type(available) is not int
            or not 0<=source<=available<=stamp
            or not isinstance(identity,str) or not identity or identity in seen):
            continue
        if last is not None and source<=last:
            raise ValueError('Gripper source time regressed')
        if stamp-source>200_000_000:
            continue
        last=source;seen.add(identity)
        selected.append({'row':row,'time':source,'value':value,'sample_id':identity})
    return selected

def aperture_events(points,now):
    """Observed opening excursions >=10mm; no semantic interpretation."""
    if not points:return {'available':False},[]
    events=[];low=points[0];peak=points[0];active=False
    previous=points[0]
    for point in points[1:]:
        if point['time']-previous['time']>250_000_000:
            if active:events.append({'start':low,'peak':peak})
            low=point;peak=point;active=False
        if active and point['value']<peak['value']-.002:
            events.append({'start':low,'peak':peak});active=False;low=point;peak=point
        if point['value']<low['value']:
            low=point;peak=point;active=False
        if point['value']>=peak['value']:
            peak=point
        if peak['value']-low['value']>=.010:
            active=True
        if active and point['value']<peak['value']-.002:
            events.append({'start':low,'peak':peak});active=False;low=point;peak=point
        previous=point
    if active:events.append({'start':low,'peak':peak})
    summary={'available':len(points)>=2,'span_s':round((points[-1]['time']-points[0]['time'])/1e9,3),
        'minimum_mm':round(min(p['value'] for p in points)*1000,2),
        'maximum_mm':round(max(p['value'] for p in points)*1000,2),
        'path_mm':round(sum(abs(b['value']-a['value']) for a,b in zip(points,points[1:]))*1000,2),
        'opening_excursions':[{'age_s':round((now-e['peak']['row']['sample_monotonic_ns'])/1e9,3),
            'from_mm':round(e['start']['value']*1000,2),'to_mm':round(e['peak']['value']*1000,2)} for e in events[-2:]],
        'meaning':'Measured aperture only, not holding/release proof'}
    return summary,events

def anchor_pair(catalog,episode,row,sample):
    mono=row['sample_monotonic_ns'];domain=sample['stamps']['camera_source_index_v1']['clock_domain']
    current_mono=sample['stamps']['camera_source_index_v1']['sample_monotonic_ns']
    if type(mono) is not int or not 0<=mono<=current_mono or row.get('clock_domain')!=domain:
        return None,None
    index=catalog._history_image_index(episode);out=[];identity=[]
    for role in ('front','right'):
        camera=row['cameras'].get(role,{});timing=camera.get('source_timing',{})
        source,available=timing.get('source_monotonic_ns_estimate'),camera.get('available_monotonic_ns')
        current=sample['stamps']['camera_source_index_v1']['cameras'][role]['source_timing']['sample_id']
        if (timing.get('clock_domain')!=domain or type(source) is not int or type(available) is not int
            or not 0<=source<=available<=mono or mono-source>100_000_000
            or not isinstance(timing.get('sample_id'),str) or not timing['sample_id'] or timing['sample_id']==current):
            return None,None
        frame=index[role].get(camera.get('wall_ns'))
        if frame is None:return None,None
        raw=catalog._history_image_bytes(episode,role,frame)
        out.append('data:image/jpeg;base64,'+base64.b64encode(raw).decode('ascii'))
        identity.append({**frame,'role':role,'sample_id':timing['sample_id'],'source_monotonic_ns':source,
                         'available_monotonic_ns':available,'anchor_sample_monotonic_ns':mono})
    return out,identity

def payload_for(catalog,sample,episode_id,variant):
    if variant not in VARIANTS:raise ValueError('Unfrozen experiment')
    payload,derivation=phase_payload(sample,'temporal_phase_mosaic_trim_v1')
    payload['questions']=deepcopy(QUESTION)
    metadata={'variant':variant,'image_derivation':derivation,'event_anchor':None}
    if variant=='definition_v2':return payload,metadata
    source=sample['stamps']['camera_source_index_v1'];now=source['sample_monotonic_ns'];domain=source['clock_domain']
    episode=catalog._episodes[episode_id]
    points=valid_gripper_points(episode['history'],now,domain)
    summary,events=aperture_events(points,now)
    state=payload['state']
    # Keep observation changes; omit raw absolute joint angles from model text.
    # Full actual feedback and quality remain untouched in local receipts.
    actual=state['right_actual'];state['right_actual']={'aperture_mm':round(actual['grip_m']*1000,2),
                                                     'feedback_fresh':actual['feedback_fresh']}
    state['observed_aperture_history']=summary
    if variant in ('event_anchor_v2','event_anchor_compact_v2','dual_history_v2','wrist_detail_v2') and events:
        event=events[-1];row=event['peak']['row'];age=(now-row['sample_monotonic_ns'])/1e9
        if .25<=age<=8:
            past,identity=anchor_pair(catalog,episode,row,sample)
            if past is not None:
                recent=sample['stamps'].get('temporal_evidence',{}).get('image_pairs',[])
                distinct_recent=len(recent)==2 and all(a['sample_id']!=b['sample_id'] for a,b in zip(identity,recent))
                if variant in ('dual_history_v2','wrist_detail_v2') and len(sample['images'])==4 and distinct_recent:
                    payload['images'],derivation=dual_mosaic(past+sample['images'],wrist_detail=variant=='wrist_detail_v2')
                    state['image_order']=('TOP: event front/right then recent front/right; BELOW: CURRENT front left, CURRENT wrist right FULL SIZE.'
                        if variant=='wrist_detail_v2' else 'TOP: opening-event front/right, recent front/right. BOTTOM: CURRENT front/right.')
                else:
                    payload['images'],derivation=mosaic(past+sample['images'][-2:])
                state['past_image_gap_s']=round(age,3)
                state['past_images_available']=True
                state['past_image_selection']='Latest measured aperture opening peak, not a labelled release'
                metadata.update(image_derivation=derivation,event_anchor={'age_s':age,'state_row':row['state_row'],
                                  'images':identity,'opening_delta_m':event['peak']['value']-event['start']['value']})
    if variant in ('event_anchor_compact_v2','dual_history_v2','wrist_detail_v2'):
        motion=state['past_1s_actual']
        state={'image_order':state['image_order'],'past_image_gap_s':state.get('past_image_gap_s'),
               'actual_now':state['right_actual'],'motion_window_s':motion['actual_span_s'],
               'joint_path_deg':motion['joint_path_deg'],'joint_velocity_deg_s':motion['joint_mean_velocity_deg_s'],
               'gripper_1s':motion['gripper'],'aperture_history':summary}
        payload['state']=state
    return payload,metadata

def dual_mosaic(images,*,wrist_detail=False):
    from PIL import Image,ImageDraw
    if len(images)!=6:raise ValueError('Require event/recent/current real front/right views')
    shape=(960,600) if wrist_detail else (896,504)
    canvas=Image.new('RGB',shape);draw=ImageDraw.Draw(canvas);inputs=[]
    for i,(uri,label) in enumerate(zip(images,('EVENT front','EVENT right','RECENT front','RECENT right','CURRENT front','CURRENT right'))):
        raw=base64.b64decode(uri.split(',',1)[1],validate=True)
        if wrist_detail:
            size=(160,120) if i<4 else ((320,240) if i==4 else (640,480))
            xy=(i*160,0) if i<4 else ((0,120) if i==4 else (320,120))
        else:
            size=(224,168) if i<4 else (448,336)
            xy=(i*224,0) if i<4 else ((i-4)*448,168)
        with Image.open(io.BytesIO(raw)) as original:
            original.load()
            if original.width*3!=original.height*4:raise ValueError('Require original whole 4:3 view')
            view=original.convert('RGB').resize(size,Image.Resampling.LANCZOS)
        canvas.paste(view,xy);draw.rectangle((xy[0],xy[1],xy[0]+112,xy[1]+13),fill='black');draw.text((xy[0]+2,xy[1]+1),label,fill='white')
        inputs.append({'role':label,'original_sha256':hashlib.sha256(raw).hexdigest(),'tile_xy':list(xy),'tile_size_px':list(size)})
    buffer=io.BytesIO();canvas.save(buffer,format='JPEG',quality=95,subsampling=0);raw=buffer.getvalue()
    return ['data:image/jpeg;base64,'+base64.b64encode(raw).decode('ascii')],{
        'kind':'wrist-detail-whole-view-mosaic' if wrist_detail else 'event-recent-current-whole-view-mosaic','size_px':list(shape),'inputs':inputs,
        'output_sha256':hashlib.sha256(raw).hexdigest(),'raw_sources_modified':False}
