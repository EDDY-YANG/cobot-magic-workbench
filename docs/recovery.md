# Continuous compatibility recovery

This component repairs registered infrastructure through reviewed legacy
interfaces. It does not migrate the old boot/SHA metadata chain. The original
robot locks, USB checks, model instance checks and execution guards remain
authoritative. No model selection/loading/inference, CAN setup, enable,
calibration, STOP clearing, motion or task resubmission exists in this module.

The installation configuration is external to Git and must end in .local.json.
Copy the documented structure in server/installation.example.json into a
separately reviewed configuration. Fill the actual host identities, existing
pinned SSH alias, three roots, TimeQP implementation fingerprint, and exact
approved source manifests. Do not put keys, passwords, tokens, model files or
recording data in the configuration or repository.

Source manifests cover all Python/JSON files under legacy agilex_control/ and
TimeQP src/, vendor/, scripts/, excluding __pycache__; launcher manifest covers
the four named reviewed boot/launcher files. Extra, missing, changed or escaped
source files block repair. A changed release requires an explicitly reviewed
installation manifest update, never an automatically accepted new hash.

## Entry and boundaries

Default observation/plan:
`python3 -B -m server.reconcile --config <external installation.local.json> --state-dir <external management state> --once`

Explicit repair:
`python3 -B -m server.reconcile --config <external installation.local.json> --state-dir <external management state> --repair --once`

Continuous operation, only when registered and started by the deployment owner:
`python3 -B -m server.reconcile --config <external installation.local.json> --state-dir <external management state> --repair --watch --interval 7`

The interval is constrained to 5-10 seconds. One exclusive process lock protects
the management state. The loop writes/prints its summary only when it changes;
intent records change only around a transition. No sensor samples, raw model
responses or high-frequency logs are stored.

Absent/stale/unknown jobs, recording, saving or model lease state defer all
repairs. A legacy blocked job counts as finished only with its valid timezone-aware\nfinished_at; a bare/unfinished blocked state still defers. Active work is preserved. No model has to be loaded: two known STOPPED
models are a valid idle state.

## Recovery transitions

1. For explicit legacy boot mismatch, disconnected link or absent Hub, use the
   reviewed local_backend.prepare_session, which performs guarded boot renewal
   through boot_binding_startup and the original connect/Hub jobs. It never
   invokes auto_sync and does not delete old validation evidence.
2. Restore the CPU-only TimeQP service and its 18970 reverse SSH path only if
   absent. Verify implementation fingerprint, ready state, real process script
   path/interpreter and matching remote/local instance. Reuse healthy existing
   TimeQP; never kill or replace an occupied endpoint.
3. For a model already READY and idle, use only the original
   RealPrecisionIO.prepare_model_path to restore its missing reverse path.
   Original before/after worker/nonce checks and remote locks remain in charge.
   STOPPED models receive no path or loading action.

Only one transition executes per observation cycle. Before mutation, source
identity is rechecked, idle state is reread, and existing robot switch locks
guard TimeQP/path changes. CPU TimeQP and SSH units have stable names and a
small owned-resource registry. Unknown pre-existing units, unverified ports or
changed registry entries block; no kill/restart/port takeover is implemented.
The process identity of an existing owned unit must match its registered argv.
Normal SSH reconnection can restore a lost registered path; it never replays a
robot command.

A durable PENDING intent is written before a potentially mutating legacy call.
A timeout/disconnect or unknown outcome leaves that intent pending. Subsequent
cycles or process restarts only observe postconditions, and do not resubmit the
operation. A known completed preparation is also not blindly repeated while
old cached boot errors remain. After readiness is actually observed, a later\nindependent Hub/link loss can create a new preparation transition. Persistent failures require review of existing
jobs/receipts and management intent; deleting an intent is not automatic.

## Verification scope

`python3 -B -m unittest discover -s tests -p 'test_reconcile*.py' -v`

Tests inject observations and adapter effects; owned-unit commands are mocked.
They cover readonly planning, active/unknown deferral, boot renewal exactly
once, unknown outcome across restart, TimeQP absence, models OFF, already-ready
model path repair, unknown ports, source conflicts and owned-unit registry
rules. These are CPU software checks, not real cold-boot, USB hotplug, model,
robot or motion acceptance. This component is not started by importing it and
does not activate itself.
