"""Freeze this local source-only release after tests, without remote operations."""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):
    with path.open('x',encoding='utf8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--test-receipt',type=Path,required=True)
    parser.add_argument('--stamp',required=True);args=parser.parse_args()
    if not args.stamp or any(c not in '0123456789-' for c in args.stamp):raise ValueError('Timestamp prefix required')
    before=json.loads((ROOT/'receipts/production-before.json').read_text(encoding='utf8'))
    altered=[p for p,v in before.items() if not Path(p).is_file() or sha(Path(p))!=v]
    frozen=json.loads((ROOT/'receipts/frozen-inputs.json').read_text(encoding='utf8'))
    frozen_changed=[p for p,v in frozen.items() if not (ROOT/p).is_file() or sha(ROOT/p)!=v]
    tests=json.loads(args.test_receipt.read_text(encoding='utf8'))
    assert tests['success'] and tests['tests_run']>0 and tests['failures']==tests['errors']==0 and not tests['skipped']
    assert not tests['guard']['external_connection_attempts'] and not tests['guard']['blocked_process_attempts'] and not tests['guard']['blocked_model_imports']
    assert tests['model_calls']==tests['hardware_calls']==0 and tests['remote_connected'] is False
    for p,v in tests['source_sha256'].items():assert sha(ROOT/p)==v, ('Source changed after test',p)
    paths=[]
    for folder in ('service','pc','scripts','tests','frozen','reports'):
        paths.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and p.suffix in ('.py','.ps1','.json','.md'))
    paths.extend(p for p in ROOT.iterdir() if p.is_file() and p.name in ('service_config.json','README.md','CODEX_HANDOFF.md'))
    for path in paths:
        assert not path.is_symlink()
        if path.suffix=='.py':compile(path.read_bytes(),str(path),'exec')
    files={p.relative_to(ROOT).as_posix():sha(p) for p in sorted(set(paths))}
    record={'schema':'bf16-service-offline-freeze-v1','created_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
        'test_receipt':str(args.test_receipt.resolve()),'test_receipt_sha256':sha(args.test_receipt),
        'tests_run':tests['tests_run'],'protected_production_files':len(before),'production_changed_or_missing':altered,
        'frozen_input_changes':frozen_changed,'source_files':len(files),'remote_uploaded':False,
        'GPU_model_run':False,'robot_run':False,'online_service_acceptance':'NOT_RUN','success':not altered and not frozen_changed}
    save(ROOT/'receipts'/f'{args.stamp}-offline-freeze.json',record)
    assert record['success'], 'Original source differs; preserved failure, no source release produced'
    manifest={'schema':'tiger-bf16-service-source-manifest-v1','files':files}
    save(ROOT/'source_manifest.json',manifest)
    package=ROOT/f'{args.stamp}-BF16-RTC-独立服务源码.zip'
    with zipfile.ZipFile(package,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:archive.write(ROOT/name,name)
        archive.write(ROOT/'source_manifest.json','source_manifest.json')
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        for name,expected in files.items():assert hashlib.sha256(archive.read(name)).hexdigest()==expected
    save(ROOT/'receipts'/f'{args.stamp}-release.json',{'path':str(package),'bytes':package.stat().st_size,'sha256':sha(package),
        'source_manifest_sha256':sha(ROOT/'source_manifest.json'),'source_entries_verified':len(files),'uploaded':False,
        'contains_model_weights_or_raw_images':False})
    print(json.dumps({'success':True,'tests':tests['tests_run'],'protected':len(before),'files':len(files),'zip_bytes':package.stat().st_size}))


if __name__=='__main__':main()
