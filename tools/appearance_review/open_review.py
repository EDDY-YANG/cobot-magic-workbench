"""Open the connected inspection service when available, or start the portable copy."""
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen
import webbrowser


def available(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/__appearance/health', timeout=1) as reply:
            info=json.load(reply)
            return info.get('schema')=='cobot-native-ui-inspection-v1' and info.get('kind')=='workbench' and info.get('readonly') is True
    except (OSError, ValueError):
        return False


if __name__=='__main__':
    for port in (8952,8962):
        if available(port):
            webbrowser.open(f'http://127.0.0.1:{port}/#deploy')
            break
    else:
        script=Path(__file__).resolve().with_name('server.py')
        sys.exit(subprocess.call([sys.executable,'-I','-B',str(script),'--port','8962','--open']))
