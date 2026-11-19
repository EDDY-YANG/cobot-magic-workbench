"""Independent CPU TimeQP worker. Default inspect creates no listener."""
from pathlib import Path
import argparse
import json
import os
import signal
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args(argv)
    if not args.serve:
        print(json.dumps({'status': 'OFFLINE_TIMEQP_INSPECTION', 'listen_started': False,
                          'model_calls': 0, 'hardware_calls': 0, 'port': 18970}))
        return 0
    # CPU libraries only; independent process settings never touch model service.
    for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[key] = '1'
    from src.temporal_rpc import TimeQPService, make_server
    service = TimeQPService(); server = make_server(service)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    server.timeout = .2
    print(json.dumps({'status': 'TIMEQP_CPU_READY', 'pid': os.getpid(), **service.health()}), flush=True)
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        service.closing = True
        # A signal may close the listener only after actual CPU solve exit.
        with service.lock:
            server.server_close()
        print(json.dumps({'status': 'TIMEQP_CPU_CLOSED', 'instance_id': service.instance_id,
                          'model_calls': 0, 'hardware_calls': 0}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
