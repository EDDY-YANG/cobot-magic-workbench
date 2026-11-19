"""Bounded native-list transport helpers; metadata only, no sensor owner."""
import json
from pathlib import Path
from .storage import verify_usb


def retry_payload(store,batch_id,trial_id):
    """Load frozen USB fields, never reconstruct from truncated list summaries."""
    meta = store.retry_metadata(batch_id,trial_id)
    return {key:meta[key] for key in ('task','batch_id','trial_id','category','notes')}


def bounded_collection_receipt(receipt):
    result = receipt.get('result',{}) if isinstance(receipt,dict) else {}
    return (isinstance(result,dict) and result.get('schema') in
            ('tiger-collection-list-v1','tiger-native-list-v1') and
            len(json.dumps(receipt,ensure_ascii=False).encode('utf-8')) <= 49152)


def list_native(cfg,root,page=1,page_size=10):
    usb = verify_usb(cfg).resolve()
    root = Path(root).resolve()
    if not root.is_relative_to(usb):
        raise ValueError('Native list requires verified USB root')
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 10:
        raise ValueError('Native list page positive; page_size 1..10')
    files = sorted((root/'runs').glob('*-episode-*/operation.json'),reverse=True)
    rows = []
    for file in files[(page-1)*page_size:page*page_size]:
        row = {'episode_id':file.parent.name,'path':str(file.parent)}
        try:
            if not file.resolve().is_relative_to(usb):
                raise ValueError('Episode receipt redirected outside USB')
            value = json.loads(file.read_text(encoding='utf-8'))
            row.update({key:value.get(key) for key in ('status','started_at','ended_at','samples')})
            row.update(raw_storage=str(file.parent),task=str(value.get('task',''))[:240],
                       failure=str(value.get('failure') or '')[:256])
        except (OSError,ValueError,TypeError) as exc:
            row['read_error'] = str(exc)[:256]
        rows.append(row)
    return {'schema':'tiger-native-list-v1','episodes':rows,'count':len(files),
            'pagination':{'page':page,'page_size':page_size,'total_items':len(files),
                          'total_pages':max(1,(len(files)+page_size-1)//page_size)}}
