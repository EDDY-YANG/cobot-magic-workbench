param([string]$Config=(Join-Path $PSScriptRoot 'client.local.json'))
$ErrorActionPreference='Stop'
if(-not(Test-Path -LiteralPath $Config)){throw 'Copy client.example.json to client.local.json and set an authorized SSH alias first. No credentials are bundled.'}
$python=Get-Command python -ErrorAction SilentlyContinue
if(-not $python){$python=Get-Command python3 -ErrorAction SilentlyContinue}
if(-not $python){throw 'Python3.10+ and OpenSSH are required for this source release.'}
& $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if($LASTEXITCODE -ne 0){throw 'Python3.10 or newer is required.'}
$entry=Join-Path $PSScriptRoot 'gateway.py'
$logs=Join-Path $env:LOCALAPPDATA 'AgileXClient'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
Start-Process -FilePath $python.Source -WindowStyle Hidden -ArgumentList @('-I','-B',('"'+$entry+'"'),'--config',('"'+(Resolve-Path -LiteralPath $Config).Path+'"')) -RedirectStandardOutput (Join-Path $logs 'startup.log') -RedirectStandardError (Join-Path $logs 'startup-error.log') | Out-Null
