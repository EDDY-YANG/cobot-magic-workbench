# Right-side DAgger with role switching

Copyright (c) 2026 Yang Taige. Software and documentation use the repository MIT license.

This workbench extension implements scheme one of the AgileX human takeover guide. A rear PiPER uses follower identity while mirroring the policy's transmitted joint and gripper targets. The intervention cancels the matching policy runner, waits for its recording cleanup and writer lock release, verifies alignment, and requests leader identity for the rear arm. Human commands then pass from the independent rear CAN branch to the front arm through the existing guarded SDK writer.

## Operator controls

| Control | Behavior |
| --- | --- |
| Prepare and align rear arm | The click declares independent right-arm wiring, supported arms and a clear movement path. Requests rear follower identity, confirms new feedback, reads command limits, primes the measured pose, enables the rear arm and aligns it with the stationary front arm. Requires the existing capture Hub. |
| Pause and take over | Cancels the matching original model runner through its existing EOF control channel. Acquires the released front writer only after model and recording cleanup. Checks the measured pair, requests rear leader identity and starts the right-only correction recording. |
| Continue model | Finalizes the owned correction, restores rear follower control and alignment, and starts a new task with the original model parameters. The original runner obtains current observations for this new task. |
| Finish and save | Finalizes the owned native recording and expert input log, restores the rear follower where applicable, and releases the pair's writers. |

The policy service stays managed by the original FP32/BF16/V2 lifecycle. Intervention ends the current execution task. Continuing creates a new execution task, with its own ID and existing readiness checks.

## Interfaces

All browser operations use the existing authenticated `POST /api/jobs` endpoint and the existing session token.

| Action | Parameters |
| --- | --- |
| `dagger_prepare` | `onsite_confirmed: true` from the explicit preparation button |
| `dagger_intervene` | `binding` and `policy_job_id` from the current DAgger snapshot |
| `dagger_resume` | `binding` and `policy_job_id` |
| `dagger_finish` | `binding` and `policy_job_id`, which is null before the first policy task |

The authenticated SSH agent accepts `op=right_dagger` with an action, control-session binding and a data object. Its actions are `prepare`, `bind_policy`, `intervene`, `policy_finished`, `resume` and `finish`. The agent advertises `right-dagger-scheme1-v1` in state. The workbench checks the version and current task before forwarding transitions.

An installation adds an explicit `right_dagger` configuration containing `enabled`, a `rear` CAN entry with `interface` and `expected_serial`, and `physical_isolation_confirmed`. The existing `can.right` entry identifies the front arm. Machine-specific configuration stays outside this repository. The recorder uses `right_only_v1` and the existing front/right camera producers.

## Control and evidence

The implementation uses the installed, source-pinned official SDK for `MasterSlaveConfig`, mode requests and motion commands. The existing receive observer decodes the same 0x155/0x156/0x157 target messages exposed by the official master-arm joint-control getter. It retains kernel receipt times and local-transmission provenance. Every role operation uses a dedicated writer scope and accepts one zero-offset FA or FC request, followed by a new configuration acknowledgement.

Fresh follower feedback, measured alignment, controller bounds and existing enable/fault guards remain part of handover. The worker uses a 5 ms wait. Mode heartbeats retain an already confirmed CAN/MOVEJ mode while targets are quiet or capture RPCs complete. Actual loop timing and firmware watchdog behavior require hardware validation.

Default engineering bounds are 2 degrees joint alignment, 2 mm gripper alignment and 10 degrees tracking error. Human target-change checks use nominal rates of 30 degrees/second and 40 mm/second, with per-update allowances of 0.1 degree and 0.2 mm. The first manual command uses the alignment bound. These checks constrain accepted commands and require review against the installed robot and task. Actual motion speed and transition latency remain hardware validation items.

Each native correction includes the control-session ID, source policy job ID and the verified-USB expert log path. The bounded asynchronous log records received expert targets and successfully submitted inputs. Camera/state/raw-CAN streams remain owned by the existing recorder. The save operation reuses recording_lifecycle.stop_owned_recording. It sends the path-scoped stop request once, waits for terminal status, and reads the actual USB operation.json to compare ownership, counts, queues and failures. The raw Hub stop response is followed by this complete finalization check. A capture with zero applied human inputs retains its native files and is marked as having no valid correction.

## Validation and installation

The tests use explicitly declared hardware and capture doubles. They cover fresh/stale input, role scopes and acknowledgements, model/task binding, actual HTTP authorization and request envelopes, actual subprocess EOF cancellation, ownership contention, the device workflow, recorder identity and UI states.

Robot installation and real motion acceptance are separate from these offline checks. A power cycle requires current host, boot, USB and source verification. The workbench shows hardware validation as pending. On hardware, validate role transition feedback, initial alignment, policy synchronization, manual joint and gripper following, interruption/fault handling, recording finalization, and fresh-observation restart before treating the feature as accepted.

## Sources

The implementation follows [AgileX human takeover guide](https://agilexsupport.yuque.com/staff-hso6mo/tetraz/qp456m45tc3ki41z?singleDoc) and the [official PiPER SDK](https://github.com/agilexrobotics/piper_sdk). The active deployment retains its existing SDK version and source fingerprint.
