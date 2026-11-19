# Release workflow — first portable slice

This checkout on the 4090 is the authoritative source for the new platform.
The existing production controller is still the sole robot owner. This tooling
does not modify or start that controller and does not enable motors, clear STOP,
select models, restart services, mount USB, send robot commands or start recording.

## Implemented boundary

`tools/release.py` uses only Python 3.10+ standard-library modules. Four commands
exist: `build`, `verify`, `stage`, `inspect`. Activation and rollback of real
controllers are intentionally **not implemented**. A staged package is not an
active deployment, model acceptance, current-boot binding or physical result.

- `source` contains allowlisted source files under `client/`, `server/`,
  `web/`, `tools/`, `tests/`, `docs/`, `.github/workflows/` and the explicit
  top-level files README.md, LICENSE, .gitignore and pyproject.toml when present.
- `client` contains only `client/`: the portable SSH gateway and launchers.
  It does not contain server source, HTML/JS assets, weights or robot code.
  The browser obtains the website from the 4090 through the client's SSH tunnel.
- Real configuration belongs outside the checkout. JSON is packaged only when
  named `*.example.json` or `*.schema.json`; `*.local.json`, .env, logs,
  receipts, runs, secrets, model/data directories and binary weight formats are
  excluded. Do not put real credentials in an example or source file.
- Private-key and common GitHub-token patterns are rejected as an additional
  check. This is not a universal secret detector; review the file manifest before
  sharing and never add production secrets to Git.

The explicit file list records size and SHA-256. Canonical JSON, sorted members,
fixed ZIP timestamps and stored compression produce identical bytes for the same
selected content, version and source revision. A clean Git checkout records HEAD;
an uncommitted or dirty checkout records `source_revision: null`. The content
hash remains exact; null must not be presented as a committed release.

Manifest roles are `client`, `web`, `server_facade`, `release_tools` and
`source_support`. These describe this package's content, **not a proven
transitive dependency graph for the existing controller**. They do not authorize
reusing old model/controller PASS. Changes to request units, parameters or API
semantics must later be included in the appropriate execution contract, even if
the change is in JavaScript.

## Build and inspect on the 4090

These are Bash commands in the new source checkout, not instructions for the
robot host. No installation is required. Use a clean Git commit for an actual
shared release; the following also permits clearly marked candidate builds.

```bash
mkdir -p dist build/releases
python3 -B -m unittest discover -s tests -p 'test_release*.py' -v
python3 -B tools/release.py build --package source --version 0.1.0-alpha.1 --output dist/agilex-source-0.1.0-alpha.1.zip
python3 -B tools/release.py build --package client --version 0.1.0-alpha.1 --output dist/agilex-client-0.1.0-alpha.1.zip
python3 -B tools/release.py inspect dist/agilex-client-0.1.0-alpha.1.zip
python3 -B tools/release.py verify dist/agilex-client-0.1.0-alpha.1.zip
python3 -B tools/release.py stage dist/agilex-source-0.1.0-alpha.1.zip --destination build/releases
```

Build refuses to overwrite an existing archive. Use a new version/output filename
when changing content. Inspect the full file list before transfer. Record the
manifest SHA-256 from successful build output in a trusted release receipt.

After transfer, supply that trusted value to `verify` or `stage`:

```bash
python3 -B tools/release.py verify dist/agilex-client-0.1.0-alpha.1.zip --expected-manifest-sha256 SHA_FROM_TRUSTED_RECEIPT
```

A self-consistent manifest only proves integrity, not who published it. Transfer
the expected hash through the already verified SSH route or another trusted
channel. A private repository alone does not authenticate an arbitrary ZIP.

Stage checks symlinks, path traversal, portable filenames, duplicate ZIP members,
exact file membership and every size/hash, then verifies extracted bytes before
renaming a temporary directory to its release ID. Retrying the same release
verifies and reuses it; an altered existing directory is rejected and preserved.
The tool never updates an existing staged release in place. This is a tool
invariant, not OS-level immutability: an account with write permission can still
change files. Verify the staged directory immediately before future use.
Interrupted staging may leave a `.stage-*` directory and per-release lock; inspect them before manual recovery. They are never activated.
There is no automated cleanup of historical packages or failures.

For a different PC, obtain **only** the client ZIP, verify it against the trusted
receipt, extract into a new versioned directory and follow `client/README.md`.
Python 3.10+ and OpenSSH are prerequisites. No GPU, model weights, Python ML stack
or local website copy is required. The client local configuration remains outside
the package. Closing its browser/tunnel does not stop a robot task.

## Concrete next upgrade sequence

1. Finish the current first slice, inspect its allowlisted source, run relevant
   offline checks, and create a local Git commit. A private GitHub remote can be
   added/pushed only under explicit publication authorization; no remote is
   created by this tool.
2. Build a source package and a separate client package from that clean commit.
   Verify on the 4090 and after client transfer. Keep controller models and raw
   data in their existing approved external locations.
3. Add a release registry and desired/active version reporting. The new
   management server may serve a verified fixed staged root later. Preserve the
   sole current task owner and present disconnected/unknown state truthfully.
4. Integrate an activation operation with the **same owner lock that admits new
   tasks**. Under that lock, confirm no running/queued/unknown job, recording,
   saving or model lease; check source/API compatibility, real host, Boot ID,
   actual imports, USB identity, model instance and service/tunnel identities.
   A prior HTTP "idle" snapshot is insufficient.
5. Once integrated, stage first, recheck under lock, atomically change a
   host-local active pointer, reload only affected components and read back the
   actual version. Cross-host changes need explicit intermediate states and
   failure recovery; do not claim a distributed atomic transaction.
6. Implement rollback through that same gate: select a verified previous package,
   keep STOP and model/owner state, re-establish valid current binding and obtain
   the evidence required by actual dependency changes. Do not copy old PASS or
   automatically resume any physical task.

Package identity and current runtime identity remain separate. Boot ID, USB
identity, running process identity and per-run evidence belong in a separately
verified binding/receipt, not in the immutable source manifest. A boot mismatch
must use reviewed restoration; replacing a JSON field alone is never recovery.

Model weights stay outside Git and source/client packages. A future external
model catalog should record trusted file sizes/SHA, normalization/config/tokenizer
contract and archive location; uploading a weight is not loading it. Loading,
acceptance, physical task outcome and final saved data remain distinct states.

## CI scope and action pins

The included GitHub workflow runs offline Python standard-library tests and
JavaScript syntax and UI state tests with required Node, on GitHub-hosted Ubuntu only.
After those checks, it builds separate source/client ZIPs for 0.1.0-alpha.1 and
uploads two candidate workflow artifacts, each with its build receipt and trusted
manifest hash. The client artifact contains only the client ZIP/receipt; users do
not need to download the server source artifact. Artifact retention is 14 days.

It has read-only repository permission and no production secrets, self-hosted
robot runner, SSH deployment, hardware access or cloud activation. Workflow
artifacts are candidates, not official GitHub Releases or activation records.
Creating/publishing a GitHub Release requires a human review and separate explicit
authorization; the workflow does neither.

Action SHAs were resolved read-only against their official upstream repositories
on 2026-10-28:

- actions/checkout v4: 11d5960a326750d5838078e36cf38b85af677262
- actions/setup-python v5: a26af69be951a213d495a4c3e4e4022e16d87065
- actions/upload-artifact v4: ea165f8d65b6e75b540449e92b4886f43607fa02

The committed workflow pins these exact SHAs. Re-resolve and review upstream
changes before any future pin update.

