# COBOT MAGIC Workbench — User manual

Yang Taige · Ye Hanzhi | Emerge Robotics

## Scope and release status

This is a research workbench in active development. This release contains a portable Windows SSH client, management server, the laboratory operator/device source, FP32/BF16 inference adapters, V2 and shared-runner source snapshots, offline checks and packaging tools. The client is portable; the laboratory source still requires installation-specific environments and configuration. The hardware workflows depend on an installed task and device backend.

The current portable package is intended for an operator connecting to an already configured GPU-host server. A complete first-time COBOT MAGIC installation remains under development.

Authors: **Yang Taige and Ye Hanzhi**. Organization: **Emerge Robotics**. Affiliations: **HKU Faculty of Engineering** and **Tam Wing Fam Innovation Wing**.

## Install the operator client

- Download the Windows SDK from the GitHub release, extract the ZIP, and double-click `Start-Workbench.cmd`. Python and OpenSSH are included. Open the recorded demo immediately; no robot or account is needed for offline playback.

- Obtain your own SSH authorization from the administrator and verify the host-key fingerprint through a trusted channel.

- In the SDK start page, enter your authorized SSH alias (or user@host), optional SSH config path and server port 8910. Click Connect. The SDK stores this local connection profile; it does not bundle credentials.

- Check the target with `ssh your-gpu-host hostname`.

- Launch `client/Start-Workstation.cmd`, or run the command below. Open the printed localhost URL and inspect the server and device status.

```
python client/gateway.py --config client/client.local.json
```

The SDK start page uses 8926; its live client defaults to 8876 to coexist with older installations. The source CLI defaults to 8875. The remote management port is 8910. Choose a free local port in the private configuration. The gateway forwards the expected Host/Origin headers.

The Windows launcher stores startup logs in `%LOCALAPPDATA%/AgileXClient`. SSH failure, an incompatible server or an occupied port is shown as a connection problem.

## Configure the GPU host

The GPU host owns software releases, model services and the task queue. Develop in a separate checkout and activate a verified fixed release. The management service listens on localhost and connects to the installed task backend.

```
python -B -m server.main --port 8910 --legacy-origin http://127.0.0.1:8873 --web-root ./web
```

This command starts the development management layer. A fixed release also supplies its verified manifest through the documented startup options. Read the source documentation `docs/server-contract.md` and `docs/recovery.md` before integrating an installation.

- `GET /platform/health` reports the management release and integrity.

- `GET /platform/status` reports runtime and validation state.

- `/workbench/` serves the connected operational interface.

- `/api/state` reports the installed task backend state.

A complete lab installation has its device backend and model/controller environments. Their full automatic installation is ongoing work. Supply addresses, SSH authorization and physical USB identities in private configuration.

## Inspect before a trial

- Read the actual host and boot identities of the GPU host and robot computer.

- Check the registered CAN adapters, bitrate, control mode and fresh joint/gripper feedback.

- Read the required camera views from the existing shared producer.

- Select the physical USB drive and verify its identity, writable mount and free space.

- Inspect the active task, recording/save status, execution owner and STOP state.

- Confirm the selected checkpoint, prompt, controller route and matching acceptance record.

Operator confirmations belong to the current trial. An explicit execution click supplies the operator declaration, and the backend also validates measurable conditions. A model, device or software change can require a fresh check.

## Operate a trial

### Collect a demonstration

- Enter the reviewed leader–follower mode.

- Confirm the task label, camera views and recording destination.

- Start recording and demonstrate the task while watching feedback.

- Stop the trial, wait for recording finalization and review the episode.

### Run a policy

- Select a deployment route, checkpoint, prompt and duration in the shared form.

- Load the selected model and complete the applicable no-motion checks.

- Inspect the real start pose, object placement and control ownership.

- Start explicitly and monitor the images, feedback and runtime messages.

- Use the operator stop when needed. Verify final saved data and label the manipulation outcome.

Current policy experiments focus on right-arm pick-and-place. Each checkpoint, prompt and controller needs its own evaluation. Record software completion and physical task success separately.

### Manual targets

The motion page uses six joint angles in degrees and gripper opening in millimetres. Model/controller internals use radians and metres. Verify the coordinate mapping and active bounds. Moving to a displayed zero pose differs from recalibrating encoder zero.

The existing manual-gripper route has an interlock with active recording/deployment. Continuous language-model action experiments require a reviewed combined execution and recording session.

## Controller routes and DAgger

The installed workbench supports FP32 + RTC, BF16 trajectory optimization and experimental Realtime-VLA V2 routes. Each route has model-identity, timing and acceptance requirements. Read exact settings from the active runtime.

Realtime-VLA V2 explores time-parameterized references and local model-predictive control. Reference coverage, paused-response handling and operator speed changes are part of the recorded experimental context.

DAgger is under development. The intended loop is a policy rollout, an explicit expert intervention, aligned correction labels, dataset aggregation, retraining and evaluation of a new checkpoint. Preserve the observation, policy proposal, expert correction and sent command as separate fields.

Add algorithms through a versioned adapter and an explicit route. Validate ownership, STOP handling, units, bounds, recording and model-instance matching before hardware use.

## Recording and replay

Raw trial data is written to the selected, verified robot USB drive. Keep the same physical storage identity through writer shutdown, then inspect the final persistence receipt and read back the streams.

- Capture video, measured joints/gripper, predictions and actual targets with timestamps.

- Save the task, checkpoint, prompt, controller settings and source revision.

- Retain original predictions alongside converted or bounded output.

- Preserve failed and interrupted trials with their actual status.

- Use manifests and hashes to associate images, metadata and commands.

Large images can be separate verified attachments while metadata remains bounded. Reuse existing producers and bounded background writers for high-frequency streams.

## Troubleshooting

- **SSH timeout.** Check power, link carrier, current address and routing; preserve host-key verification.

- **Stale page or feedback.** Read the current backend response and source age before submitting another task.

- **Acceptance is stale.** Check source revision, host boot, USB and model instance; rebuild affected bindings through the reviewed process.

- **STOP latched.** Inspect the session and use explicit recovery. The software stop blocks new targets; a previously accepted target may still finish.

- **Reference coverage insufficient.** Inspect timing evidence and retain the accepted trajectory. Recovery checks the complete RTC prefix before advancing.

- **Recording incomplete.** Keep the failed receipt, inspect USB identity and final flush, and resolve the writer failure.

A timed-out command has an unknown outcome. Query the original task and reconcile ownership before another action. Reconnecting must not replay a motion request.

## Source and release workflow

The repository separates `client/`, `server/`, `web/`, `site/`, `docs/`, `tools/` and `tests/`. The public project page lives in `site/`; the operational management UI lives in `web/`.

Develop in a branch, run relevant checks and commit. Add a concise change description and screenshots for visible updates. Build a fixed package, verify it and deploy to an idle installation. Read back the actual runtime version.

```
python -B -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_ui*.mjs
python -B tools/release.py build --package source --output dist/source.zip
python -B tools/release.py verify dist/source.zip
```

Use example configuration in source control. Personal addresses, credentials, raw recordings, weights and runtime receipts stay outside public history. This Alpha package provides a management/client foundation for a configured lab stack; portable robot installation is ongoing.

Code and documentation use MIT. Third-party dependencies retain their own licenses. The visual theme follows Yang Taige's Quest 3 / Dual E6 guide. These system diagrams are original, editable schematics.

## Asset and replay provenance

The full COBOT MAGIC reference is linked at AgileX mobile_aloha_sim commit `f799ae0192c0c2bbd502ec4d9bbec8c2633acd1a`. Its v2 branch declares BSD without complete license text; the meshes and derived renders are excluded from this release. The separate master-branch MIT license was not assumed to cover them.

The public MuJoCo replay uses MIT PiPER geometry from `piper_ros @ ac41fcbcdda598f01b51cf6175ed9a24d0dacadc`. Measured J1–J6 values are converted to radians for forward kinematics. This is not contact simulation or a calibrated whole-machine digital twin. Gripper aperture is numerical because its geometric mapping is uncalibrated.

The 2026-10-24 source run ended on an evidence-queue error. Native camera/state streams were retained; task success is not scored. The 46.83-second export uses native state-to-frame references. Two camera views and model rendering share one video clock; hashes and timing notes accompany the demo.
