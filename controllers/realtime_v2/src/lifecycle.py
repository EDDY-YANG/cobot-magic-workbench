"""Ownership-preserving cancellation: local timeout is not server completion."""
import time


def drain_before_unlock(model, worker=None, *, emit=lambda _: None, retry_wait=.1):
    """Keep caller's locks/recording until server AND local worker are finished.

    A transport outage is recorded, not treated as a successful release. The
    operator may interrupt the whole process, but normal stop does not abandon
    a live GPU operation or release another owner's lease.
    """
    model.cancel()
    attempts = 0
    while True:
        attempts += 1
        try:
            receipt = model.close(timeout=15.)
            if worker is not None and not worker.done():
                emit({'event': 'DRAIN_WAIT_LOCAL_WORKER', 'attempt': attempts})
                time.sleep(retry_wait)
                continue
            return {**receipt, 'drain_attempts': attempts}
        except Exception as exc:
            emit({'event': 'DRAIN_RETAINS_OWNERSHIP', 'attempt': attempts,
                  'error_type': type(exc).__name__, 'credentials_logged': False})
            time.sleep(retry_wait)
