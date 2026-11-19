"""Compile unmodified official t_renderer0.2.1 on the target's existing glibc.

Requires an already prepared Rust toolchain. No Rust installer, apt, sudo,
glibc update, model call, solver-math edit or version-check bypass is present.
Build source/target in exec tmpfs; crates/cache/logs/artifact stay private USB.
"""
from pathlib import Path,PurePosixPath
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from build_linux_acados import verify_usb,mount_info,sha,require

COMMIT='a480a64b0a2cc15d4b1e6146e986388709ac0716'
SOURCE_SHA='092885d0b5d8dfe95d9acf23d38b6c9674dde0909d3d096e1285b7ef854f592a'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',required=True)
    parser.add_argument('--acados-root',required=True)
    parser.add_argument('--source-archive',required=True)
    parser.add_argument('--cargo',required=True,help='Absolute existing private cargo binary; no install is attempted')
    parser.add_argument('--usb-mount',required=True)
    parser.add_argument('--usb-uuid',required=True)
    parser.add_argument('--ram-root',required=True)
    parser.add_argument('--replace-renderer-sha256',help='Explicit expected hash when replacing an incompatible private renderer')
    parser.add_argument('--cargo-cache', help='Already fetched cache inside this independent USB root')
    parser.add_argument('--lockfile', help='Already resolved lockfile inside this independent USB root')
    args=parser.parse_args()
    require(sys.platform=='linux','Run only on the target Linux host')
    root=Path(args.runtime_root).resolve(strict=True);usb=Path(args.usb_mount).resolve(strict=True)
    require(root.is_relative_to(usb) and root!=usb,'Private runtime must be on USB')
    identity=verify_usb(usb,args.usb_uuid)
    acados=Path(args.acados_root).resolve(strict=True)
    require(acados.is_relative_to(root),'Cannot modify any production acados installation')
    archive=Path(args.source_archive).resolve(strict=True)
    require(archive.is_relative_to(root) and sha(archive)==SOURCE_SHA,'Official pinned source archive SHA differs')
    cargo=Path(args.cargo).absolute()
    require(cargo.is_absolute() and cargo.is_file(),'Existing absolute cargo executable required')
    ram=Path(args.ram_root).resolve(strict=True);mi=mount_info(ram)
    require(mi['fstype']=='tmpfs' and 'noexec' not in mi['options'].split(',') and os.access(ram,os.W_OK),
            'Writable executable tmpfs required; no SSD fallback')
    require(shutil.disk_usage(ram).free>512*1024*1024,'At least512MiB free exec tmpfs required for Rust build')
    destination=acados/'bin/t_renderer'
    if destination.exists():
        require(args.replace_renderer_sha256 and sha(destination)==args.replace_renderer_sha256.lower(),
                'Existing renderer replacement needs its exact explicit SHA')
    stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d-%H-%M-%S')
    receipts=root/'receipts';receipts.mkdir(exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix='tiger-renderer-',dir=ram)).resolve()
    receipt={'schema':'tiger-native-glibc-compatible-renderer-build-v1','status':'STARTED',
        'official_commit':COMMIT,'source_archive_sha256':SOURCE_SHA,'runtime_root':str(root),
        'ram_work':str(work),'USB':identity,'model_calls':0,'hardware_commands':0,
        'glibc_modified':False,'math_or_version_check_modified':False}
    log=receipts/(stamp+'-renderer-native-build.log')
    try:
        with tarfile.open(archive,'r:gz') as tar:
            members=tar.getmembers();seen=set()
            require(len(members)<100 and sum(x.size for x in members)<1024*1024,'Unexpected source archive size')
            for member in members:
                path=PurePosixPath(member.name)
                require(path.parts and path.parts[0]=='tera_renderer-'+COMMIT and not path.is_absolute()
                    and '..' not in path.parts and '\\' not in member.name
                    and member.name not in seen and (member.isdir() or member.isfile()),'Unsafe source member')
                seen.add(member.name)
                target=work.joinpath(*path.parts)
                if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
                target.parent.mkdir(parents=True,exist_ok=True)
                with tar.extractfile(member) as inp,target.open('xb') as out:
                    shutil.copyfileobj(inp,out)
        source=work/('tera_renderer-'+COMMIT)
        require('version = "0.2.1"' in (source/'Cargo.toml').read_text(),'Original version must remain0.2.1')
        source_hashes={p.relative_to(source).as_posix():sha(p) for p in source.rglob('*') if p.is_file()}
        temp=work/'tmp';temp.mkdir()
        cache=Path(args.cargo_cache).resolve(strict=True) if args.cargo_cache else root/'deps/renderer-cargo-cache'
        require(cache.is_relative_to(root), 'Cargo cache must stay in private runtime')
        cache.mkdir(parents=True,exist_ok=True)
        env=os.environ.copy();env.update(CARGO_HOME=str(cache),CARGO_TARGET_DIR=str(work/'target'),
            TMPDIR=str(temp),TMP=str(temp),TEMP=str(temp),CARGO_BUILD_JOBS='2',CUDA_VISIBLE_DEVICES='')
        env['PATH']=str(cargo.parent)+os.pathsep+env.get('PATH','')
        commands=[[str(cargo),'generate-lockfile'],[str(cargo),'build','--release','--locked','--jobs','2']]
        if args.lockfile:
            lockfile=Path(args.lockfile).resolve(strict=True)
            require(lockfile.is_relative_to(root), 'Resolved lockfile must be in private runtime')
            shutil.copy2(lockfile,source/'Cargo.lock')
            receipt['input_lockfile_sha256']=sha(lockfile)
            env['CARGO_NET_OFFLINE']='true'
            commands=[[str(cargo),'build','--release','--locked','--offline','--jobs','2']]
        receipt['cargo_version']=subprocess.check_output([str(cargo),'--version'],env=env,text=True).strip()
        receipt['glibc_version']=subprocess.check_output(['getconf','GNU_LIBC_VERSION'],text=True).strip()
        with log.open('x') as output:
            # Upstream0.2.1 has no lockfile. Resolve once, preserve, then --locked.
            for command in commands:
                require(verify_usb(usb,args.usb_uuid)==identity,'USB identity changed')
                output.write(json.dumps(command)+'\n');output.flush()
                completed=subprocess.run(command,cwd=source,env=env,stdout=output,stderr=subprocess.STDOUT)
                require(completed.returncode==0,'Renderer build failed; original source and log retained')
        for rel,expected in source_hashes.items():
            require(sha(source/rel)==expected,'Official renderer source was modified: '+rel)
        binary=work/'target/release/t_renderer'
        require(binary.is_file() and binary.read_bytes()[:4]==b'\x7fELF','Native Linux ELF renderer missing')
        version=subprocess.check_output([str(binary),'--version'],text=True).strip()
        require(version=='t_renderer 0.2.1','Compiled original renderer version differs')
        # Real rendering smoke, not just --version. No acados math involved.
        templates=work/'smoke';templates.mkdir();(templates/'proof.in').write_text('{{ value }}')
        context=work/'context.json';context.write_text('{"value":"ORIGINAL_TERA_NATIVE_OK"}')
        rendered=work/'rendered.txt'
        subprocess.run([str(binary),str(templates/'*'),'proof.in',str(context),str(rendered)],check=True)
        require(rendered.read_text()=='ORIGINAL_TERA_NATIVE_OK','Real original Tera rendering failed')
        glibc_symbols=subprocess.check_output(['readelf','--version-info',str(binary)],text=True)
        requirements=sorted(set(re.findall(r'GLIBC_([0-9.]+)',glibc_symbols)),key=lambda s:tuple(map(int,s.split('.'))))
        lock_copy=receipts/(stamp+'-renderer-Cargo.lock');shutil.copy2(source/'Cargo.lock',lock_copy)
        require(verify_usb(usb,args.usb_uuid)==identity,'USB changed before publication')
        destination.parent.mkdir(exist_ok=True)
        staged=destination.parent/('t_renderer.new-'+stamp)
        require(not staged.exists(),'Staging name already exists')
        shutil.copy2(binary,staged);staged.chmod(0o755)
        require(sha(staged)==sha(binary),'Renderer copy hash differs')
        # Execute from final USB filesystem too; noexec mounts fail here.
        require(subprocess.check_output([str(staged),'--version'],text=True).strip()==version,
                'Renderer cannot execute from private USB')
        backup=None
        if destination.exists():
            require(sha(destination)==args.replace_renderer_sha256.lower(),'Existing renderer changed during build')
            backup=destination.with_name('t_renderer.incompatible-glibc-'+stamp)
            require(not backup.exists(),'Backup already exists')
            destination.rename(backup)
        staged.rename(destination)
        receipt.update(status='ORIGINAL_RENDERER021_NATIVE_BUILD_AND_RENDER_PASS',version=version,
            renderer_sha256=sha(destination),glibc_symbol_requirements=requirements,
            original_source_sha256=source_hashes,cargo_lock_sha256=sha(lock_copy),
            backup=None if backup is None else str(backup),destination=str(destination))
        require(work.parent==ram and work.name.startswith('tiger-renderer-'),'RAM cleanup target differs')
        shutil.rmtree(work);receipt['ram_work_cleaned']=True
    except BaseException as exc:
        receipt.update(status='FAILED_RETAINED',error=type(exc).__name__+': '+str(exc),ram_work_cleaned=False)
        raise
    finally:
        verify_usb(usb,args.usb_uuid)
        path=receipts/(stamp+'-renderer-native-build.json')
        path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'status':receipt['status'],'receipt':str(path)},ensure_ascii=False),flush=True)

if __name__=='__main__':
    main()
