"""Open the original operator pages in Edge with a local presentation-only extension.

This launcher does not implement an HTTP server, submit a job or replace any
API endpoint. SSH, when needed, carries the original loopback listeners.
"""
from hashlib import sha256
import http.client
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
FORWARDS = {8873: 'workbench', 8891: 'review'}
MARKERS = {
    'workbench': (b'id="page-deploy"', b'id="stop-button"', b'id="page-dagger"'),
    'review': (b'id="verdict-buttons"', b'id="record-list"', b'id="settings-dialog"'),
}


def original_listener(port, kind):
    """Distinguish a missing listener from an occupied, unrecognized one."""
    # On Windows a closed localhost port can time out before Winsock reports
    # connection refusal. A short exclusive bind checks availability without
    # starting a listener. Occupied ports must still identify the original UI.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as error:
            if error.winerror not in (10013, 10048):
                raise
        else:
            return False
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=6)
    try:
        conn.request('GET', '/')
        reply = conn.getresponse()
        page = reply.read(512 * 1024)
        if (reply.status != 200 or not all(s in page for s in MARKERS[kind])
                or b'data-ui-review="true"' in page):
            raise RuntimeError(f'Port {port} is occupied by an unrecognized interface.')
        return True
    finally:
        conn.close()


def ensure_original_links():
    missing = [p for p, kind in FORWARDS.items() if not original_listener(p, kind)]
    if not missing:
        return
    config = Path.home()/'.ssh'/'lab-4090-only'/'config'
    ssh = shutil.which('ssh.exe')
    if not config.is_file() or not ssh:
        raise RuntimeError('Original lab SSH configuration and Windows OpenSSH are required.')
    command = [ssh, '-F', str(config), '-N', '-T', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
               '-o', 'ConnectTimeout=6', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=3']
    for port in missing:
        command.extend(['-L', f'127.0.0.1:{port}:127.0.0.1:{port}'])
    command.append('lab-4090')
    child = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+12
        while time.monotonic()<deadline:
            if all(original_listener(p, FORWARDS[p]) for p in missing):
                return
            if child.poll() is not None:
                break
            time.sleep(.2)
        raise RuntimeError('The original 4090 workbench or data-review service is unavailable.')
    except BaseException:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        raise


def prepare():
    extension = HERE/'Appearance'
    digest = sha256()
    for member in ('manifest.json', 'operator-theme.css', 'operator-theme.js'):
        digest.update((extension/member).read_bytes())
    candidates = [Path(os.environ[key])/'Microsoft'/'Edge'/'Application'/'msedge.exe'
                  for key in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA') if key in os.environ]
    edge = next((p for p in candidates if p.is_file()), None)
    if edge is None:
        raise RuntimeError('Microsoft Edge is required for this Windows launcher.')
    # These are the existing application ports, including native links between them.
    ensure_original_links()
    port = 8875 if original_listener(8875, 'workbench') else 8873
    profile = Path(os.environ['LOCALAPPDATA'])/'CobotMagicWorkbench'/'AppearanceProfiles'/digest.hexdigest()[:12]
    url = f'http://127.0.0.1:{port}/#deploy'
    return edge, extension, profile, url


def main():
    if sys.platform != 'win32':
        raise RuntimeError('Use this launcher on the operator Windows PC.')
    edge, extension, profile, url = prepare()
    command = [str(edge), '--no-first-run', '--no-default-browser-check',
               '--user-data-dir='+str(profile), '--load-extension='+str(extension),
               '--new-window', url]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    print('Native workbench:', url)
    print('Native data review: http://127.0.0.1:8891/')
    print('Actions use the real application and its original permissions.')


if __name__=='__main__':
    try:
        main()
    except (OSError, RuntimeError) as error:
        print('Unable to open the original workbench:', error, file=sys.stderr)
        sys.exit(1)
