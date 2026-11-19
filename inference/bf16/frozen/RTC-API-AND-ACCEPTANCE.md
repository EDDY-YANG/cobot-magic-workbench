# Native32 FP32 H50 RTC / 原生32维FP32实时分块

Approved18Hz playback / R14 /600ms candidate. Approval JSON SHA-256:
f14a05080543c879ed4ce895e20e87a4e91507ab1562ed5133cc6c163027e2dc.
Native training remains30Hz. Execution playback is18Hz, not a retrained18Hz model.
Robot replan14 points gives14/18s and18/14Hz; freeze12/18s,35 known points
ending34/18s, reserve23/18s. Send50Hz and mechanical limits remain unchanged.

This supersedes the earlier native-only portions of MODEL-SUPERVISOR-MIGRATION.md.
本文件覆盖旧迁移说明中仅支持native推理的部分。当前仍是候选，CPU通过不代表GPU或真机通过。

Nine runtime files must be uploaded together, using runtime-SHA256.json:
model_supervisor.py, worker_entry.py, gpu_inventory.py, gpu_telemetry.py,
prepare_supervisor.py, launch_supervisor.py, supervisor_control.py, rtc_core.py,
fp32_rtc_http.py. The original15:38 loader and HTTP source remain unchanged.

The worker shares the original FP32Policy parameters, transforms, normalization,
graph and native sampler. New RTC uses native50x32 noise and32-dimensional VJP
through the denoiser at all10 integration steps. Known cotangent error is masked
to35 time points and7 physical coordinates. Other latent coordinates are NOT
forced to zero. Visual/text KV is cached outside VJP. The approved schedule is
full weight0..11, exponential decrease12..34, zero35..49; beta cap5. No prefix
overwrite or output blending pretends that guided predictions are executed points.

Worker startup separately audits native and RTC JAXPR/StableHLO for all-FP32 and
HIGHEST dot/conv; the old native audit cannot stand in for RTC. It also compares
zero-mask raw32 results with the same native RNG/noise using bitwise array_equal,
absolute/relative tolerance0. Both NPY arrays and SHA evidence are retained even
on failure. Startup does not self-certify latency, memory or robot quality.

## RTC wire

POST /infer remains the original native endpoint; idle shadow compatibility is
preserved. New POST /infer_rtc requires the existing two deployment lease headers
and original four JSON observation fields plus rtc:

```json
{
  "schema": "tiger-fp32-h50-rtc-v1",
  "enabled": true,
  "previous_actions": "exactly35x7 actual accepted future reference points, absolute rad/m",
  "previous_valid_steps": 35,
  "freeze_steps": 12,
  "execution_label_hz": 18,
  "native_training_hz": 30,
  "beta": 5.0,
  "trial_index": 0,
  "request_index": 1
}
```

Bootstrap is enabled:false, previous_actions:[], previous_valid_steps:0, with the
same frozen schedule settings. Unknown time/dimensions are internally zero-error
masked, never repeated last targets. trial_index is zero-based; trial_seed/key_seed
is42+trial_index. Request key is fold_in(key(trial_seed),request_index), permitting
exact per-request replay. Index must increase; gaps after local source rejection
are allowed, repeated/reversed indices and a different trial are rejected. No
inference retry changes these rules.

POST /lease/acquire accepts session_id, step, trial_index. /status's inactive lease
always exposes active:false/session_id:null/step:null/expires_in_s:null. Tokens
remain memory-only; only acquire exposes one to its owner. RTC replies preserve
actions[50,7], dtype and timing; add rtc with actual enabled/valid/trial/request/
seed/key policy/prefix SHA, plus contract_fingerprint. Health exposes rtc support
and acceptance_binding:{contract_fingerprint,identity,semantics}. Identity binds normalization,
tokenizer, source commit, RTC implementation and separate RTC audit, original loader,
loaded worker entry and loaded supervisor code. Copy this binding rather than
guessing local paths or hashing a different process's source.

The approved deferred-USB contract adds the exact semantics object to that fingerprint:
`{"timing_basis":"packet_ready_to_validated_ram_commit_ready","storage_semantic_version":"tiger-h50-bounded-ram-async-usb-finalflush-v1","execution_contract_version":"tiger-h50-native30-playback18-r14-d600-v1","native_training_hz":30,"execution_label_hz":18,"replan_steps":14,"reply_deadline_ms":600,"acceptance_p95_ms":550,"acceptance_max_ms":600}`.
Old receipts without these semantics or with the prior synchronous durable-write
basis,30Hz playback or old300/350ms contract are stale/rejected. Model precision, RTC math, steps and numerical outputs do
not change. The robot uses its approved bounded RAM journal (128MiB/2048 records),
then flushes all input/result/native records to the same verified128GB USB before
declaring the trial complete. Full USB audit failure remains failure; no SSD fallback.

## Whole-GPU telemetry

The supervisor uses read-only libnvidia-ml.so.1 via ctypes at100ms, outside the
robot50Hz loop. Raw samples stay in a bounded RAM ring; no high-rate disk log is
written. Model-load cumulative peak and peak sample survive later lower-use leases.
Lease release/expiry/close saves raw CSV+JSON once with SHA and returns gpu_telemetry.
NVML unavailable/error, overwritten lease samples, or stale/gapped samples fail;
there is no JAX-allocator replacement or invented0. Sample freshness QA is300ms
for the100ms instrument, distinct from robot control/latency parameters. Peaks are
sampled whole-device memory, including desktop users; sub-sample transients remain
unobserved. GET /telemetry reports current sampled status. Source lease must close
normally for acceptance; expiry/manager-close cannot be relabelled as a healthy run.

## Self-service acceptance publication / 自助验收发布

GET /acceptance?step=N reads receipts/acceptance-step-N.json. Missing is NOT_RUN;
not-current step is UNVERIFIED_CURRENT_MODEL; changed code/RTC IR/binding is STALE.
There is no readiness claim from candidate presence or startup warmup alone.

After its original run lease is released and local USB/source audits pass, trusted
robot CLI acquires a NEW short lease for the same selected step and POSTs
/acceptance/submit with the lease headers. Body:

- schema:tiger-fp32-h50-rtc-acceptance-v1, step, source_run_session_id,
  source_trial_index, and exact live acceptance_binding's contract_fingerprint/identity/semantics.
- metrics.raw_latencies_ms:100 actual packet-ready→RTC validation→RAM plan commit-ready
  elapsed values, in the same order as metrics.rtc_call_request_indices.
- metrics.rtc_call_request_indices:100 unique ascending measured indices;
  metrics.warmup_request_indices:3 unique preceding warmup indices.
- results.robot_chain_audits_passed:true only after real local audit.
- results.timing_basis:exactly packet_ready_to_validated_ram_commit_ready.
- results.usb_finalflush_validated:true only after the bounded asynchronous journal
  is closed, fully flushed within the approved300s close timeout, and all original
  USB input/result/native recordings pass the trusted robot's recorded-file audit.
- evidence: actual USB/source/journal/audit manifest paths and SHA records.

Do not include tokens in this body. source_run_session_id refers to the original
103-call run, not the new publication lease. The manager independently matches its
RAM records for all3+100 returned calls (no hidden additional failures), requires
the100 measured calls to have RTC enabled, validates FP32 H50 output and response
SHA, and rejects a client latency shorter than the worker's own measured interval.
It recomputes linear-interpolated P95 and max from the100 raw values, reads its own
source-run NVML evidence and hashes, and uses current actual worker FP32 RTC audit
and exact-zero-mask proof. It does not trust submitted status/P95/max or a body-only
claim that100 VJP calls occurred. Robot source/hardware audit remains attributed to
the trusted robot CLI; manager does not open a robot or claim independent actuation.

Fixed requirements:3warmup,100measured enabled RTC calls, P95<=550ms, max<=600ms,
max(source lease peak, model-load sampled peak)<=22528MiB. Any missing proof or
over-budget value is FAIL/rejection. There is no precision fallback, threshold
relaxation or fake success. Every publish writes an immutable UUID history and
atomic current receipt; previous successful current files are preserved by SHA
history before replacement. This operation cannot select models or start motion.

Failed acceptance is safe to retain and is not permission to start hardware.
The earlier30Hz/300/350ms failures remain historical failures. New thresholds
apply only to new measurements with the new binding, not old receipt relabelling.
Root owns the actual5k rollout, GPU IR/equivalence readback and100 real-observation
no-motion end-to-end measurements. This subagent has performed CPU tests and the
earlier explicitly allowed read-only GPU owner audit only; it has not remotely
loaded, selected, inferred, installed, stopped34980, or sent robot commands.
