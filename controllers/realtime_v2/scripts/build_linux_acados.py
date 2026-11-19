"""Build pinned CPU acados in executable tmpfs, publish a private USB prefix.

No pip, apt, sudo, SSH, GPU, model, hardware or mounting operations. Requires an
already mounted verified USB and writable executable tmpfs. Logs/receipts are
persisted on that USB. symlinks are dereferenced when publishing to exFAT.
"""
from datetime import datetime,timezone,timedelta
from pathlib import Path,PurePosixPath
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

COMMITS={'.':'503364817c872d474ab5bed219c26760ac267769',
    'external/blasfeo':'d6251233923c9b475fe894fb729fb63ab693e301',
    'external/hpipm':'e3a56c1caddd7f12d125d84f337b9a9e5c186271'}

def require(ok,message):
    if not ok:
        raise RuntimeError(message)

def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()

def mount_info(path):
    data=json.loads(subprocess.check_output(['findmnt','--json','--target',str(path),
        '--output','TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN,UUID'],text=True))
    rows=data.get('filesystems',[])
    require(len(rows)==1,'A single actual mount identity is required')
    return rows[0]

def verify_usb(mount,expected_uuid):
    info=mount_info(mount)
    require(Path(info['target']).resolve()==mount,'USB path must be the actual mountpoint')
    require(str(info.get('uuid','')).upper()==expected_uuid.upper(),'Actual USB UUID differs')
    require('rw' in info['options'].split(','),'USB must be mounted read/write')
    physical=Path('/sys/dev/block')/info['maj:min']
    resolved=str(physical.resolve(strict=True))
    require('/usb' in resolved,'Target is not verified as a physical USB block device')
    require(os.access(mount,os.W_OK),'USB is not writable by this user')
    return {**info,'physical_sysfs':resolved}

def safe_extract(archive,destination):
    """Only bounded regular acados files; reject all link/special/escape entries."""
    destination=Path(destination).resolve()
    with tarfile.open(archive,'r:gz') as tar:
        members=tar.getmembers();seen=set();total=0
        require(0<len(members)<=20000,'Invalid archive member count')
        for member in members:
            path=PurePosixPath(member.name)
            require(not path.is_absolute() and path.parts and path.parts[0]=='acados'
                and '..' not in path.parts and '\\' not in member.name,
                'Archive path escapes the private source root')
            require(member.name not in seen,'Duplicate archive entry')
            seen.add(member.name)
            require(member.isfile() or member.isdir(),'Archive links/special files are not permitted')
            total+=member.size
            require(member.size>=0 and total<=512*1024*1024,'Archive expands beyond source budget')
            target=destination.joinpath(*path.parts)
            require(target.resolve().is_relative_to(destination),'Archive target escaped')
        require(shutil.disk_usage(destination).free>total*3+128*1024*1024,
                'Insufficient tmpfs free space for source/build/install peak')
        for member in members:
            target=destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True,exist_ok=True)
                continue
            target.parent.mkdir(parents=True,exist_ok=True)
            stream=tar.extractfile(member)
            require(stream is not None,'Missing archive file stream')
            with stream,target.open('xb') as output:
                shutil.copyfileobj(stream,output)
            target.chmod(0o755 if member.mode&0o111 else 0o644)
    source=destination/'acados'
    provenance=json.loads((source/'SOURCE-PROVENANCE.json').read_text())
    require(provenance.get('commits')==COMMITS,'Source commit set differs from frozen build')
    expected=provenance['source_sha256']
    actual={p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    require(actual==set(expected)|{'SOURCE-PROVENANCE.json'},'Archive has missing/extra source files')
    for rel,expected_sha in expected.items():
        require(sha(source/rel)==expected_sha,'Source content hash differs: '+rel)
    return source,provenance

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',required=True)
    parser.add_argument('--archive-sha256',required=True)
    parser.add_argument('--runtime-root',required=True)
    parser.add_argument('--usb-mount',required=True)
    parser.add_argument('--usb-uuid',required=True)
    parser.add_argument('--ram-root',default='/run/user/'+str(os.getuid()) if hasattr(os,'getuid') else '')
    parser.add_argument('--prefix',help='New absolute private prefix; default runtime-root/deps/acados')
    args=parser.parse_args()
    require(sys.platform=='linux','Linux-only builder; do not execute on the PC')
    require(platform.machine() in ('x86_64','amd64'),'This frozen generic CPU build is for x86_64')
    runtime=Path(args.runtime_root).resolve(strict=True)
    usb=Path(args.usb_mount).resolve(strict=True)
    require(runtime.is_relative_to(usb) and runtime!=usb,'Private runtime must be inside selected USB')
    identity=verify_usb(usb,args.usb_uuid)
    archive=Path(args.archive).resolve(strict=True)
    require(archive.is_relative_to(runtime),'Use the source archive staged in this private USB runtime')
    require(len(args.archive_sha256)==64 and sha(archive)==args.archive_sha256.lower(),'Archive SHA256 mismatch')
    prefix=(Path(args.prefix) if args.prefix else runtime/'deps/acados').resolve()
    require(prefix.is_relative_to(runtime) and prefix!=runtime and not prefix.exists(),
            'Private prefix must be new; an existing build is never overwritten')
    ram=Path(args.ram_root).resolve(strict=True);ram_info=mount_info(ram)
    options=ram_info['options'].split(',')
    require(ram_info['fstype']=='tmpfs' and 'noexec' not in options and os.access(ram,os.W_OK),
            'Build needs writable executable tmpfs; /dev/shm is often noexec. No SSD fallback.')
    for program in ('cmake','make','gcc','g++'):
        require(shutil.which(program),'Prepare compiler tool before build: '+program)
    tag=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d-%H-%M-%S')
    receipts=runtime/'receipts';receipts.mkdir(exist_ok=True)
    log_path=receipts/(tag+'-linux-acados-build.log')
    receipt_path=receipts/(tag+'-linux-acados-build.json')
    work=Path(tempfile.mkdtemp(prefix='tiger-v2-acados-',dir=ram)).resolve()
    receipt={'schema':'tiger-private-linux-acados-build-v1','status':'STARTED',
        'source_archive_sha256':args.archive_sha256.lower(),'commits':COMMITS,
        'USB':identity,'ram_build_path':str(work),'prefix':str(prefix),'no_model_calls':True,
        'hardware_commands':0,'production_environment_modified':False,'steps':[]}
    started=time.monotonic();stage=None
    try:
        source,provenance=safe_extract(archive,work)
        build=work/'build';temp=work/'tmp';temp.mkdir()
        env=os.environ.copy();env.update(TMPDIR=str(temp),TMP=str(temp),TEMP=str(temp),
            OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',CUDA_VISIBLE_DEVICES='')
        configure=['cmake','-S',str(source),'-B',str(build),'-G','Unix Makefiles',
            '-DCMAKE_BUILD_TYPE=Release','-DCMAKE_INSTALL_PREFIX='+str(source),
            '-DBLASFEO_TARGET=GENERIC','-DHPIPM_TARGET=GENERIC','-DBUILD_SHARED_LIBS=ON',
            '-DACADOS_WITH_OPENMP=OFF','-DACADOS_WITH_SYSTEM_BLASFEO=OFF',
            '-DACADOS_EXAMPLES=OFF','-DACADOS_UNIT_TESTS=OFF','-DACADOS_INSTALL=ON',
            '-DBLASFEO_EXAMPLES=OFF','-DBLASFEO_TESTING=OFF','-DBLASFEO_BENCHMARKS=OFF','-DHPIPM_TESTING=OFF']
        configure.extend('-DACADOS_WITH_'+name+'=OFF' for name in ('QPOASES','DAQP','HPMPC','QORE','OOQP','QPDUNES','OSQP','CLARABEL'))
        with log_path.open('x') as log:
            for label,cmd in (('configure',configure),('compile',['cmake','--build',str(build),'--','-j2']),
                              ('install',['cmake','--install',str(build)])):
                require(verify_usb(usb,args.usb_uuid)==identity,'USB identity changed during build')
                step_start=time.monotonic();print(label,flush=True)
                log.write('\n'+label+': '+json.dumps(cmd)+'\n');log.flush()
                completed=subprocess.run(cmd,cwd=work,env=env,stdout=log,stderr=subprocess.STDOUT)
                receipt['steps'].append({'name':label,'exit_code':completed.returncode,
                    'seconds':time.monotonic()-step_start})
                require(completed.returncode==0,'acados '+label+' failed; inspect preserved USB log')
        for name in ('libacados.so','libhpipm.so','libblasfeo.so'):
            path=source/'lib'/name
            require(path.is_file(),'Missing installed CPU library: '+name)
            require(path.read_bytes()[:4]==b'\x7fELF','Installed library is not Linux ELF: '+name)
        for p in source.rglob('*'):
            if p.is_symlink():
                require(p.resolve(strict=True).is_relative_to(source),'Installed symlink escapes private source')
        require(verify_usb(usb,args.usb_uuid)==identity,'USB identity changed before publish')
        prefix.parent.mkdir(parents=True,exist_ok=True)
        stage=prefix.parent/(prefix.name+'.incomplete-'+tag)
        require(not stage.exists(),'Incomplete staging path already exists')
        # BLASFEO uses SOVERSION. Dereference all library links for exFAT.
        shutil.copytree(source,stage,symlinks=False)
        require(not any(p.is_symlink() for p in stage.rglob('*')),'USB installation must have no symlinks')
        libs={p.name:sha(p) for p in (stage/'lib').iterdir() if p.is_file() and '.so' in p.name}
        for name in ('libacados.so','libhpipm.so','libblasfeo.so'):
            require(sha(stage/'lib'/name)==sha(source/'lib'/name),'Published library content differs')
        require(verify_usb(usb,args.usb_uuid)==identity and not prefix.exists(),'Publish identity/target changed')
        stage.rename(prefix);stage=None
        receipt.update(status='LINUX_ACADOS_BUILT_AND_PUBLISHED',libraries_sha256=libs,
            symlinks_dereferenced=True,source_file_count=provenance['files'],
            elapsed_seconds=time.monotonic()-started,
            tools={tool:subprocess.check_output([tool,'--version'],text=True).splitlines()[0]
                   for tool in ('cmake','gcc','g++','make')},
            python=sys.version,renderer_prepared=False,
            next_step='Prepare official Linux t_renderer>=0.2.1 and install matching acados_template in private venv; then build_solver.py')
        # Remove only this exact new RAM directory after a verified USB copy.
        require(work.parent==ram and work.name.startswith('tiger-v2-acados-'),'RAM cleanup target mismatch')
        shutil.rmtree(work)
        receipt['ram_work_cleaned']=True
    except BaseException as exc:
        receipt.update(status='FAILED_RETAINED',error=type(exc).__name__+': '+str(exc),
            elapsed_seconds=time.monotonic()-started,ram_work_cleaned=False,
            incomplete_usb_staging=None if stage is None else str(stage))
        raise
    finally:
        # A missing USB identity is an error, never an internal-SSD fallback.
        verify_usb(usb,args.usb_uuid)
        receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'status':receipt['status'],'receipt':str(receipt_path),
                          'prefix':str(prefix)},ensure_ascii=False),flush=True)

if __name__=='__main__':
    main()
