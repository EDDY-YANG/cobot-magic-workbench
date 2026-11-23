# Right-pair common-target trial

Copyright (c) 2026 Yang Taige

## Current experimental result

The first live role request received a 0x476/0x70 response, but the trial
aborted after three initial target triplets. Its last joint feedback preceded
the role request. A fresh post-role feedback barrier is required before any
further operational use; a fixed 0.1-second wait is not sufficient. No zero
endpoint or physical pair control was accepted. The historical USB artifact
and receipt remain unchanged.

This isolated experiment invokes the installed `piper-sdk 0.6.2` role API
`MasterSlaveConfig(0xFC, 0, 0, 0)` once, then requests CAN/MOVEJ position-speed
control and sends a bounded common six-zero trajectory on the verified right bus.
It does not replace production control, change limits, clear STOP, reset, disable,
calibrate, move a gripper, restore teaching, or automatically retry.

A separately supplied private contract binds host, boot, CAN adapter, SDK source
SHA-256 and runtime helper source hashes. Keep that contract and live raw results
off public GitHub. The existing hardware writer lock and verified USB storage
are used. An active recording/task blocks the experiment.

The receiver uses a fresh bounded raw-CAN session, with kernel timestamps,
overflow detection and CAN error rejection. Historical Hub failures are retained.
Because two controllers can publish the same feedback IDs, the trial checks at
most two persistent anonymous value groups per joint-pair ID. Each group retains
the existing 0.5-degree stationarity bound. All observed groups must lie within
the existing OEM handover's 2-degree proximity bound. They are not labelled
leader or follower. This cannot independently certify either physical identity.

The common start reference is the midpoint of the observed bounds. This can
produce a small initial convergence; it is not four independent exact-position
holds. Controller angle-limit replies are intersected with existing documented
bounds. The cosine reference uses 5 deg/s and 25 deg/s2; native speed is 10%.
These are command/reference constraints, not measured physical-speed guarantees.
No motor is automatically enabled: every observed driver must already be healthy
and enabled. Unexpected other target writers, CAN errors, stale/invalid feedback,
lost USB/STOP, excess tracking error, and deadlines terminate further sends.

The endpoint report is explicitly bus-level with physical pair confirmation
pending. It is not a DAgger acceptance or a validated seamless-switch claim.

Default execution is receive-only inspection. Actual `--apply` requires separate
explicit operator authorization for both arms' swept workspace and right-pair
motion. Raw evidence is saved only through the runtime's USB-bound Session.

Offline checks:
```bash
python -B -m unittest discover -s tests -p test_right_pair_can_trial.py
```
Unit fixtures are synthetic validator tests, not robot evidence.
