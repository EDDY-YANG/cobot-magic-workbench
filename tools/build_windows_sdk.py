"""Build a portable Windows SDK from verified official runtime archives."""
from pathlib import Path
import argparse,hashlib,json,shutil,zipfile
ROOT=Path(__file__).resolve().parents[1]
SOURCES={
 'python-3.14.10-embed-amd64.zip':('90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159','https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip'),
 'OpenSSH-Win64-10.4.0.0p1.zip':('b67ff8becd203502190ec42ac7787c24c2cd3325ed7700485fe7a1c83d2a7957','https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.4.0.0p1-Preview/OpenSSH-Win64.zip')}
def main():
 p=argparse.ArgumentParser();p.add_argument('--downloads',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 stage=a.output/'cobot-magic-sdk-windows-x64';stage.mkdir(parents=True,exist_ok=False)
 for name,(sha,url) in SOURCES.items():
  file=a.downloads/name;assert hashlib.sha256(file.read_bytes()).hexdigest()==sha,name
  with zipfile.ZipFile(file) as z:
   assert z.testzip() is None
   destination=stage/'runtime'/('python' if name.startswith('python') else 'openssh');destination.mkdir(parents=True)
   for n in z.namelist():
    if n.endswith('/'):continue
    relative=Path(n) if name.startswith('python') else Path(*Path(n).parts[1:])
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe archive member')
    target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(n))
 for directory in ['client','site']:
  shutil.copytree(ROOT/directory,stage/directory,ignore=shutil.ignore_patterns('__pycache__','*.local.json','*.log'))
 for file in ['LICENSE','THIRD_PARTY_NOTICES.md']:shutil.copyfile(ROOT/file,stage/file)
 (stage/'Start-Workbench.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\n"%~dp0runtime\\python\\python.exe" "%~dp0client\\sdk.py"\r\nif errorlevel 1 pause\r\n',encoding='ascii')
 (stage/'README.txt').write_text('COBOT MAGIC Workbench v0.1.3\n\nExtract the complete ZIP. Double-click Start-Workbench.cmd.\nOpen the recorded demo without a robot or a Python installation.\nConnect uses your own authorized SSH alias/config and verified host key.\nDefault SDK port: 8926. Live client: 8876. GPU management server: 8910.\nClosing the client does not stop a running physical trial. Use the workbench Stop action.\nThe package never starts a model, clears STOP or sends robot motion automatically.\nRuntime licenses are under runtime/python and runtime/openssh.\nOpenSSH vendor package tag: 10.4.0.0p1-Preview.\n\nAuthors: Yang Taige, Ye Hanzhi / Emerge Robotics\n',encoding='utf8')
 manifest={p.relative_to(stage).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.rglob('*') if p.is_file()}
 (stage/'manifest.json').write_text(json.dumps({'version':'0.1.3','runtime_sources':SOURCES,'files':manifest},indent=2))
 output=a.output/(stage.name+'.zip')
 with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for f in sorted(stage.rglob('*')):
   if f.is_file():z.write(f,str(Path(stage.name)/f.relative_to(stage)))
 with zipfile.ZipFile(output) as z:assert z.testzip() is None
 print(json.dumps({'zip':str(output.resolve()),'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'files':len(manifest)}))
if __name__=='__main__':main()
