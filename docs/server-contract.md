# Management facade contract

This first implementation is a compatibility facade and read-only management
projection. It does not replace legacy boot guards or implement automatic
reconciliation. It contains no model loading, robot SDK, camera, CAN writer,
STOP clearing or implicit job submission.

Run from the platform repository:
`python3 -m server.main --port 8910 --legacy-origin http://127.0.0.1:8873 --web-root ./web --release-id <reviewed-release-id>`.
Only 127.0.0.1 can be bound. The default release ID is
`development-unreleased`; integrity is UNVERIFIED unless startup verifies the actual staged source release.
For a staged release, launch from that stage with python3 -B -m server.main,
--release-manifest ./release-manifest.json and optionally
--expected-manifest-sha256 <trusted-sha256>. The verifier reuses tools.release.verify;
actual imported server modules and tools/release.py must belong to that same
payload and --web-root must be its web/ directory. A specified invalid manifest,
pin, imported path or web root fails startup before a listener is created.
Verified identity reports VERIFIED_AT_STARTUP and the actual manifest release ID,
manifest SHA and whether an external SHA pin was checked. This proves startup
file consistency, not continuous protection against later filesystem changes.
No manifest retains UNVERIFIED; a SHA without a manifest is an error.
Disable bytecode writes (-B or PYTHONDONTWRITEBYTECODE=1) because unexpected
files in a staged payload are correctly rejected by full membership verification.

## Routes

- GET /: new web/index.html.
- GET /platform-assets/<path>: local web/ assets with resolved path containment.
- GET /workbench/ and /workbench/<path>: legacy UI, prefix removed.
- GET /api/* and other root static assets: compatibility proxy to the fixed
  legacy loopback origin. Redirects are refused, never followed.
- GET /platform/health: schema agilex-platform-health-v1, protocol_version 1,
  service agilex-control-platform, release_id, server_instance_id,
  readiness management_only. No legacy, model or robot request is performed.
- GET /platform/status: schema agilex-platform-status-v1. Reads legacy
  /api/state only. One cache entry, maximum one second, single-flight reads.
- POST only: /api/jobs, /api/stop, /api/v2/speed, /api/storage/scan,
  /api/storage/select, /api/semantic/replay, /api/semantic/cancel.

POST requires the exact facade Host and Origin, one X-Tiger-Token, one
Content-Length, no Transfer-Encoding, a JSON object and at most 16 KiB.
Host and Origin are rewritten to the fixed legacy origin, and the token and
body are forwarded once. Legacy remains responsible for token validation,
actions, all admission checks and execution. A disconnect may mean the POST
was accepted: the facade reports an unknown/failed transport result and
never retries. No redirects or target-selected upstream origins are allowed.

## Status projection

Fields:
- upstream: status ONLINE/OFFLINE/PROTOCOL_ERROR, fresh, age_s, error_code.
- software_release: schema agilex-software-release-v1, release_id,
  integrity UNVERIFIED or VERIFIED_AT_STARTUP, manifest_sha256, manifest_pin_verified, scope management_facade.
- runtime_session: schema agilex-runtime-session-v1, status OBSERVED/UNKNOWN.
  components.robot_link/hub/shared_runner/v2_runner each expose status
  READY/BLOCKED/UNKNOWN and reason_code. components.models.FP32/BF16 expose
  READY/OFF/ACTIVE/UNKNOWN and nullable step. An intentionally STOPPED model
  maps to OFF and does not itself create a failure.
- components.storage: status, verified, nullable uuid/mountpoint/free_bytes.
  Values are copied only when legacy usb.verified is true, connected is true,
  and the explicitly selected USB identity is present. No scan/mount occurs.
- ownership: ACTIVE/IDLE/UNKNOWN plus bounded active_jobs (id/action/state).
  This is observed legacy metadata, not a new lease or exclusion primitive.
- boot: nullable robot_boot_id, status OBSERVED/UNKNOWN, host and host_boot_id.
  host fields describe this facade host, not AgileX.
- network: READY means the legacy HTTP state endpoint was read; it does not
  claim all robot/model/TimeQP links are healthy.
- validation_record: schema agilex-validation-summary-v1, status
  VALID/NOT_VALIDATED/UNKNOWN, source legacy_guard, motion_verified false.
  VALID only projects the legacy V2 NO_SEND validated flag with a fresh,
  ready V2 profile. It is not physical task success or new admission.
- legacy_guard: authoritative true, execution_admission_granted false,
  allowlisted capabilities (action/available/reason_code).
- current_failure: nullable code/component, derived from current observations.
- historical_failure: nullable last failed/cancelled/interrupted job identity,
  context HISTORICAL_JOB_NOT_CURRENT_CONNECTION. Raw error strings are excluded.

Status copies only documented fields. It excludes CSRF token, configuration,
credentials, raw error prose, paths other than explicitly verified USB mount,
raw receipts and complete legacy state. Old /api/state remains available to
the legacy workbench under its original semantics.

On failed, malformed or redirected upstream state, all upstream-dependent
components, models, ownership, robot boot, network and validation become
UNKNOWN. Cached READY is not served after its maximum age. Local facade
identity remains available independently. Stale legacy precision/V2
subcomponents also become UNKNOWN.

## Bounds and tests

Legacy connection/read inactivity timeout defaults to 3 seconds. State reads
are limited to 4 MiB and a checked overall read deadline; at most one final
socket timeout can extend that deadline. Client request inactivity timeout is
5 seconds. Proxy responses stream in 64 KiB chunks, at most 256 MiB with a
30-second checked deadline, and may terminate early on failure; Range and
If-Range are forwarded. Local management assets are limited to 8 MiB.
No response content or high-rate logs are written to disk.

Run `python3 -m unittest discover -s tests -p 'test_server*.py' -v`.
Tests use temporary local fixture HTTP servers only. They do not exercise
production services, models, boot recovery, USB hardware or physical motion.
