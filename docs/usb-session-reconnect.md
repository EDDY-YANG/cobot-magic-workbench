# USB session reconnect

A USB drive can return under a new Linux device number or physical port. The runtime correctly rejects the old frozen mount identity. Previously the web session could retain an old Hub owner in RAM after disconnection, leaving the original drive permanently locked even after the actual producer exited.

The existing **Connect AgileX** action now handles that expired session. Automatic storage polling remains read-only. The server entry adapter:

1. Requires the original UUID and mountpoint to be the unique eligible candidate. A different disk, duplicate UUID, read-only mount or ambiguous inventory fails.
2. Confirms that no local job is queued, running or unknown, and normally closes the old monitoring bridge. An unconfirmed close fails.
3. Verifies the real robot host, current boot, USB identity, delivered source hashes and non-storage configuration. It obtains the existing camera, web-agent, Hub-launch and CAN-writer locks and requires the Hub port to refuse connections and known control processes to be absent.
4. Keeps that remote lease while the original storage-only configuration preparation and source readback run. The remote lease has a 90-second hard deadline and releases on an explicit close, EOF or failure; local operations reserve time before each bounded step.
5. Releases the lease before the original connect starts the new monitoring agent. A failure keeps recovery pending and requires a new check on retry. Previous job failures stay in history.

A still-running Hub is an active producer even when it is not recording. This change reports that owner and refuses rebinding; it does not stop the Hub, recording, motion, model or clear STOP. Stop the producer through the operator workflow before retrying. Software ownership locks are advisory and do not prove that an unrelated legacy application or physical leader is silent.

The extension is server-only. It does not replace robot source, CAN mappings, controller metadata or previous acceptance. Current USB connection success is separate from model loading and motion/recording admission.

## Installation

Publish these files together in an immutable release:

- tools/serve_operator_appearance.py
- tools/workbench_usb_recovery.py
- tools/usb_reconnect_probe.py

The external installation's pins must include both recovery helpers and the original entry pins. Startup refuses missing helper pins or changed pinned bytes. Existing DAgger handling, appearance and right-arm feedback guards are retained. The /api/state response reports usb_reconnect.module_sha256, usb_reconnect.probe_sha256 and the pending flag for readback.

Do not put machine configuration, live USB receipts, tokens or runtime acceptance in the public repository.

## Validation

Run the relevant offline tests:

    python -B -m unittest discover -s tests -p 'test_usb_reconnect.py'
    python -B -m unittest discover -s tests -p 'test_operator_appearance.py'

Tests cover expired cache, exact-identity reinsertion, producer refusal, unknown tasks, bridge close failure, ambiguous disks, mid-check identity change, held locks during preparation, failed retries and the original handler's lock-release window. They do not constitute physical unplug/replug or recording acceptance.
