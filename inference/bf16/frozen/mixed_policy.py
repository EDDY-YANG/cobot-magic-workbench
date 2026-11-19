"""Offline candidate: BF16 backbone, FP32 action heads and guided-RTC math.

Import is CPU/device independent. Construction explicitly loads a GPU model;
the runner must first acquire the existing GPU-owner locks. No HTTP or robot I/O.
Production/frozen files are never edited. Real GPU/quality validation is pending.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "frozen_source"
MANIFEST = FROZEN / "2026-10-20-13-15-source-manifest.json"
MANIFEST_SHA = "e6e7ecd4d7cfadb5cb821b0399968e641054006449ba45d9832834e17b4ef5e5"
SCHEMA = "tiger-pi05-bf16-backbone-f32-rtc-offline-v1"
F32_SCOPES = ("rtc_f32_clean", "rtc_f32_error", "rtc_f32_finish", "rtc_f32_euler")


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_frozen_sources():
    if file_sha(MANIFEST) != MANIFEST_SHA:
        raise RuntimeError("Frozen source manifest identity changed")
    rows = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    for row in rows:
        if row.get("missing") or file_sha(FROZEN / row["name"]) != row["sha256"]:
            raise RuntimeError("Frozen source identity changed: " + row["name"])
    return rows


def _load_frozen(name, filename):
    spec = importlib.util.spec_from_file_location(name, FROZEN / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SOURCE_ROWS = verify_frozen_sources()
_loader = _load_frozen("tiger_bf16_frozen_loader", "fp32_loader.py")
# Only this uniquely named, frozen-copy module is changed. Its unchanged
# bootstrap checks the experiment filesystem/uv.lock and imports its source copy.
_loader.JOB = ROOT
_rtc = _load_frozen("tiger_bf16_frozen_rtc", "frozen_rtc_core.py")
bootstrap = _rtc.bootstrap
validate_rtc = _rtc.validate_rtc


def verify_runtime_sources():
    """Hash the isolated source and reject any already-imported foreign copy."""
    source_root = ROOT / "source"
    for row in SOURCE_ROWS:
        if not row["name"].startswith("src__"):
            continue
        parts = row["name"].split("__")
        expected = (source_root.joinpath(*parts)).resolve()
        if _loader.sha(expected) != row["sha256"]:
            raise RuntimeError("Experiment runtime source differs: " + row["name"])
        module_name = ".".join(parts[1:-1] + [parts[-1][:-3]])
        module = sys.modules.get(module_name)
        if module is not None and Path(getattr(module, "__file__", "")).resolve() != expected:
            raise RuntimeError("Imported runtime source is outside the experiment: " + module_name)


def parameter_target_dtype(path):
    """Keep small FP32 projections and learned norm scales/biases unchanged."""
    names = [str(getattr(part, "key", getattr(part, "idx", part))) for part in path]
    if "PaliGemma" not in names:
        return "float32"
    lower = [name.lower() for name in names]
    if any("norm" in name for name in lower) and lower[-1] in ("scale", "bias"):
        return "float32"
    return "bfloat16"


def summarize_parameters(jax, tree):
    leaves, elements = Counter(), Counter()
    for value in jax.tree.leaves(tree):
        if hasattr(value, "dtype"):
            dtype = str(value.dtype)
            leaves[dtype] += 1
            elements[dtype] += int(value.size)
    return {"leaf_dtypes": dict(leaves), "elements_by_dtype": dict(elements),
            "floating_elements": sum(v for k, v in elements.items()
                                     if "float" in k or k in ("bf16", "f16"))}


def _require_f32(value, name):
    if str(value.dtype) != "float32":
        raise RuntimeError(name + " must be float32, got " + str(value.dtype))
    return value


def guided_velocity_fp32(velocity_fn, latent, denoising_time, previous, weights, *, beta=5.0):
    """Same frozen RTC equations; only the network may use lower precision.

    VJP inputs/outputs are FP32. Its internal BF16 network Jacobian is approximate;
    this is deliberately NOT described as an all-FP32 gradient implementation.
    """
    import jax
    import jax.numpy as jnp
    for name, value in (("latent", latent), ("time", denoising_time),
                        ("previous", previous), ("weights", weights)):
        _require_f32(value, name)

    def estimate_clean(value):
        velocity = _require_f32(velocity_fn(value), "network velocity boundary")
        with jax.named_scope("rtc_f32_clean"):
            clean = value - denoising_time * velocity
        return clean, velocity

    clean, pullback, velocity = jax.vjp(estimate_clean, latent, has_aux=True)
    with jax.named_scope("rtc_f32_error"):
        cotangent = (previous - clean) * weights  # weights applied exactly once
    correction = _require_f32(pullback(cotangent)[0], "VJP correction boundary")
    with jax.named_scope("rtc_f32_finish"):
        tau = jnp.float32(1.0) - denoising_time
        denominator = jnp.where(tau > 0, denoising_time * tau, jnp.float32(1.0))
        scale = jnp.where(tau <= 0, jnp.float32(beta),
                          jnp.minimum((denoising_time ** 2 + tau ** 2) / denominator,
                                      jnp.float32(beta)))
        result = velocity - scale * correction
    return _require_f32(result, "guided velocity")


def sample_actions_mixed(model, rng, observation, previous, weights, *,
                         num_steps=10, preprocess=None, attention_mask=None):
    """Frozen H50 sampler structure, FP32 noise/carry and a genuine per-step VJP."""
    import jax
    import jax.numpy as jnp
    if (num_steps, model.action_horizon, model.action_dim) != (10, 50, 32):
        raise ValueError("Candidate freezes H50/dim32/10 denoising steps")
    _require_f32(previous, "previous")
    _require_f32(weights, "weights")
    if preprocess is None:
        from openpi.models import model as model_api
        preprocess = lambda obs: model_api.preprocess_observation(None, obs, train=False)
    if attention_mask is None:
        from openpi.models.pi0 import make_attn_mask
        attention_mask = make_attn_mask
    noise = jax.random.normal(rng, (observation.state.shape[0], 50, 32), dtype=jnp.float32)

    def native(_):
        # Keep the original native sampler, same native32 noise, without guidance.
        return _require_f32(model.sample_actions(rng, observation, num_steps=num_steps,
                                                noise=noise), "native output")

    def guided(_):
        obs = preprocess(observation)
        prefix, p_mask, p_ar = model.embed_prefix(obs)
        _, cache = model.PaliGemma.llm([prefix, None], mask=attention_mask(p_mask, p_ar),
                                     positions=jnp.cumsum(p_mask, axis=1) - 1)
        cache = jax.tree.map(jax.lax.stop_gradient, cache)
        delta = jnp.float32(-1.0 / num_steps)

        def step(carry):
            latent, denoising_time = carry

            def velocity_fn(value):
                suffix, s_mask, s_ar, cond = model.embed_suffix(
                    obs, value, jnp.broadcast_to(denoising_time, (value.shape[0],)))
                prefix_mask = jnp.broadcast_to(p_mask[:, None, :],
                                               (value.shape[0], suffix.shape[1], p_mask.shape[1]))
                full_mask = jnp.concatenate([prefix_mask, attention_mask(s_mask, s_ar)], axis=-1)
                positions = jnp.sum(p_mask, axis=-1)[:, None] + jnp.cumsum(s_mask, axis=-1) - 1
                (_, output), _ = model.PaliGemma.llm([None, suffix], mask=full_mask,
                    positions=positions, kv_cache=cache, adarms_cond=[None, cond])
                # FP32 head + explicit boundary; casting never replaces the VJP.
                return jnp.asarray(model.action_out_proj(output[:, -50:]), dtype=jnp.float32)

            velocity = guided_velocity_fp32(velocity_fn, latent, denoising_time, previous, weights)
            with jax.named_scope("rtc_f32_euler"):
                updated = latent + delta * velocity
                updated_time = denoising_time + delta
            return _require_f32(updated, "Euler latent"), _require_f32(updated_time, "Euler time")

        return jax.lax.while_loop(lambda carry: carry[1] >= -delta / jnp.float32(2.0),
                                  step, (noise, jnp.float32(1.0)))[0]

    return jax.lax.cond(jnp.any(weights != 0), guided, native, operand=None)


def audit_jaxpr(closed, *, require_rtc_scopes=True):
    """Structural audit usable with a tiny CPU graph; no model is instantiated."""
    dtypes, primitives, precisions, scoped = Counter(), Counter(), Counter(), Counter()
    dot_types = Counter()
    seen = set()

    def dtype(value):
        return str(getattr(getattr(value, "aval", None), "dtype", ""))

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif hasattr(value, "jaxpr"):
            walk(value.jaxpr)
        elif hasattr(value, "eqns") and id(value) not in seen:
            seen.add(id(value))
            for var in list(value.invars) + list(value.outvars) + list(value.constvars):
                dtypes[dtype(var)] += 1
            for eq in value.eqns:
                name = eq.primitive.name
                primitives[name] += 1
                types = [dtype(v) for v in list(eq.invars) + list(eq.outvars)]
                dtypes.update(types)
                stack = str(getattr(getattr(eq, "source_info", None), "name_stack", ""))
                for scope in F32_SCOPES:
                    if scope in stack:
                        scoped[scope] += 1
                        if any(t in ("bfloat16", "float16", "float64") for t in types):
                            raise RuntimeError("Non-FP32 RTC outer math in " + scope)
                if name in ("dot_general", "conv_general_dilated"):
                    raw = eq.params.get("precision")
                    precisions[repr(raw)] += 1
                    if not _loader.precision_is_highest(raw):
                        raise RuntimeError("Dot/conv must retain explicit HIGHEST precision")
                    dot_types["|".join(types)] += 1
                walk(eq.params)

    walk(closed)
    if any(dtypes[t] for t in ("float16", "float64")):
        raise RuntimeError("FP16/FP64 is outside this mixed contract")
    bf16_dots = sum(n for key, n in dot_types.items() if "bfloat16" in key)
    if not bf16_dots:
        raise RuntimeError("No BF16 dot/conv found; a final output cast is not BF16 inference")
    outputs = [dtype(v) for v in closed.jaxpr.outvars]
    if outputs != ["float32"]:
        raise RuntimeError("Mixed model output must be one float32 latent tensor")
    if require_rtc_scopes and set(scoped) != set(F32_SCOPES):
        raise RuntimeError("Missing explicit FP32 RTC outer math scopes")
    return {"jaxpr_dtype_counts": dict(dtypes), "primitives": dict(primitives),
            "dot_conv_precision_counts": dict(precisions), "dot_conv_dtype_counts": dict(dot_types),
            "bf16_dot_conv_nodes": bf16_dots, "fp32_rtc_scope_equations": dict(scoped),
            "output_dtypes": outputs, "fp16_nodes": 0,
            "vjp_precision_note": "FP32 latent/cotangent/correction boundaries; internal network VJP uses mixed precision"}


def _receipt_path(path):
    result = Path(path).resolve()
    result.relative_to(ROOT.resolve())
    if result == ROOT.resolve() or FROZEN.resolve() in (result, *result.parents):
        raise ValueError("Receipts must use an isolated experiment subdirectory")
    return result


def audit_stablehlo(hlo):
    if re.findall(r"(?:(?<![A-Za-z0-9_])|(?<=x))(f16|f64)(?![A-Za-z0-9_])", hlo):
        raise RuntimeError("FP16/FP64 found in candidate StableHLO")
    count = sum("bf16" in line and ("stablehlo.dot_general" in line
                or "stablehlo.convolution" in line) for line in hlo.splitlines())
    if not count:
        raise RuntimeError("Lowered StableHLO has no BF16 dot/conv")
    return {"stablehlo_bf16_dot_conv_nodes": count}


class MixedPolicy(_loader.FP32Policy):
    """Offline only. Caller owns the GPU locks; no automatic precision fallback."""
    def __init__(self, step=5000, num_steps=10, seed=42):
        if step not in (2000, 5000, 10000, 15000, 20000) or num_steps != 10 or seed != 42:
            raise ValueError("Frozen checkpoint candidates, 10 steps and seed42 required")
        verify_frozen_sources()
        # This is the unchanged bootstrap/host/filesystem/source-lock check.
        self.jax = _loader.bootstrap()
        import numpy as np
        import flax.nnx as nnx
        import jax.numpy as jnp
        import sentencepiece
        from openpi import transforms as t
        from openpi.models import model as model_api, pi0_config, tokenizer
        from openpi.shared import normalize
        verify_runtime_sources()
        self.np, self.jnp, self.nnx, self.model_api = np, jnp, nnx, model_api
        self.step, self.num_steps, self.seed = step, num_steps, seed
        archive = _loader.ARCHIVE.resolve(strict=True)
        self.checkpoint = (archive / "models" / ("update-" + str(step).zfill(8))).resolve(strict=True)
        self.checkpoint.relative_to(archive)
        self.manifest = json.loads((archive / "download-manifest.json").read_text(encoding="utf-8"))
        expected = {row["relative"]: row for row in self.manifest["files"]}
        for name in ("candidate.json", "assets/tiger_piper_right_strict95/norm_stats.json"):
            path = self.checkpoint / name
            if _loader.sha(path) != expected[str(path.relative_to(archive))]["sha256"]:
                raise RuntimeError("Checkpoint identity/norm mismatch")
        candidate = json.loads((self.checkpoint / "candidate.json").read_text())
        if candidate["completed_updates"] != step or candidate["parameter_dtype"] != "float32":
            raise RuntimeError("Original FP32 checkpoint identity mismatch")
        self.norm_path = self.checkpoint / "assets/tiger_piper_right_strict95/norm_stats.json"
        self.norm = normalize.load(self.norm_path.parent)
        for key in ("state", "actions"):
            if any(getattr(self.norm[key], name).shape != (7,) for name in ("mean", "q01", "q99")):
                raise RuntimeError("Wrong physical normalization dimensions")
        token_path = archive / "reading-support/assets/big_vision/paligemma_tokenizer.model"
        support = json.loads((archive / "SUPPORT-READY.json").read_text())
        token_row = next(row for row in support["files"] if row["path"].endswith("paligemma_tokenizer.model"))
        if _loader.sha(token_path) != token_row["sha256"]:
            raise RuntimeError("Tokenizer identity mismatch")

        class LocalTokenizer(tokenizer.PaligemmaTokenizer):
            def __init__(self, path):
                self._max_len = 200
                self._tokenizer = sentencepiece.SentencePieceProcessor(model_proto=path.read_bytes())

        self.transforms = t.compose([t.Normalize(self.norm, use_quantiles=True), t.ResizeImages(224, 224),
            t.TokenizePrompt(LocalTokenizer(token_path), discrete_state_input=True), t.PadStatesAndActions(32)])
        self.unnormalize = t.Unnormalize(self.norm, use_quantiles=True)
        self.config = pi0_config.Pi0Config(pi05=True, dtype="bfloat16", action_horizon=50,
                                           action_dim=32, max_token_len=200)
        started = time.perf_counter()
        # Same restoration API and original dtype; host restoration avoids a full
        # FP32 GPU allocation coexisting with the derived BF16 candidate.
        params = model_api.restore_params(self.checkpoint / "params", restore_type=np.ndarray, dtype=None)
        before = _loader.FP32Policy.parameter_summary(self, params)
        if before["floating_elements"] != _loader.EXPECTED_PARAMETERS:
            raise RuntimeError("Original FP32 parameter count mismatch")

        def convert(path, value):
            if not hasattr(value, "dtype"):
                return value
            if str(value.dtype) == "float32":
                return jnp.asarray(value, dtype=jnp.dtype(parameter_target_dtype(path)))
            return jnp.asarray(value)  # Integer counters remain integer.

        params = self.jax.tree_util.tree_map_with_path(convert, params)
        model = self.config.load(params, remove_extra_params=False)
        # pi0/siglip may first be imported by config.load; verify them as well.
        verify_runtime_sources()
        del params
        self.graph, self.state = nnx.split(model)
        self.param_before = before
        self.param_after = summarize_parameters(self.jax, nnx.state(model, nnx.Param).to_pure_dict())
        del model
        if (self.param_after["floating_elements"] != _loader.EXPECTED_PARAMETERS
                or not self.param_after["elements_by_dtype"].get("bfloat16")):
            raise RuntimeError("Mixed parameter count/type contract failed")
        self.load_seconds = time.perf_counter() - started
        # Reuse the exact frozen observation and accepted-prefix preprocessing.
        self.base = SimpleNamespace(prepare=self.prepare_observation, norm=self.norm)
        graph = self.graph

        def forward(state, rng, batch):
            network = nnx.merge(graph, state)
            obs = model_api.Observation.from_dict(batch["observation"])
            return sample_actions_mixed(network, rng, obs, batch["previous32"], batch["weights32"])

        self.forward, self.jit = forward, self.jax.jit(forward)
        self.compiled, self.audit, self.compile_seconds = None, None, None
        self.metadata = {"schema": SCHEMA, "status": "LOADED_NOT_GPU_VALIDATED",
            "checkpoint_step": step, "checkpoint": str(self.checkpoint),
            "checkpoint_source_dtype": "float32", "precision": "mixed_bf16_backbone_fp32_rtc",
            "parameter_dtype": "mixed_bfloat16_float32", "compute_dtype": "mixed_bfloat16_float32",
            "matmul_precision": "highest", "tf32_override": "0",
            "backbone_compute_dtype": "bfloat16", "fp32_modules": ["action_in_proj", "action_out_proj", "time_mlp_in", "time_mlp_out"],
            "norm_parameter_policy": "FP32 learned norm scale/bias; backbone matrices BF16",
            "state_dtype": "float32", "observation_state_dtype": "float32",
            "latent_dtype": "float32", "action_dtype": "float32",
            "horizon": 50, "internal_action_dim": 32, "physical_action_dim": 7,
            "action_basis": "absolute", "units": ["rad"] * 6 + ["m"], "sampling_steps": 10,
            "seed": 42, "batch_size": 1, "robot_commands": 0, "source_commit": _loader.COMMIT,
            "normalization_sha256": _loader.sha(self.norm_path), "tokenizer_sha256": _loader.sha(token_path),
            "frozen_manifest_sha256": MANIFEST_SHA, "implementation_sha256": file_sha(__file__),
            "real_views": ["front", "right"], "absent_left_mask": False,
            "rtc_rng_policy": "fold_in(key(42+trial_index),request_index)",
            "precision_audit_passed": False, "quality_validation_passed": False,
            "hardware_instruction_trace": False}
        self.rtc = {"schema": _rtc.SCHEMA, "implementation_schema": SCHEMA,
            "precision_audit_passed": False, "precision_audit_sha256": None,
            "freeze_steps": _rtc.FREEZE, "previous_valid_steps": _rtc.PREVIOUS,
            "execution_label_hz": _rtc.LABEL_HZ, "native_training_hz": _rtc.NATIVE_TRAINING_HZ,
            "beta": _rtc.BETA, "vjp_precision_note": "BF16 network differentiation; FP32 outer RTC math"}
        # An explicit nonrecursive base view supports the common offline runner.
        # base.prepare is the original observation transform, self.prepare adds RTC.
        self.base.__dict__.update(jax=self.jax, jnp=self.jnp, np=self.np, nnx=self.nnx,
            model_api=self.model_api, state=self.state, graph=self.graph,
            unnormalize=self.unnormalize, param_before=self.param_before,
            param_after=self.param_after, load_seconds=self.load_seconds,
            metadata=self.metadata, config=self.config, seed=self.seed)

    def prepare_observation(self, data):
        return _loader.FP32Policy.prepare(self, data)

    prepare = _rtc.RTCFP32Adapter.prepare

    def audit_and_compile(self, data, receipt_dir):
        receipt_dir = _receipt_path(receipt_dir)
        batch = self.prepare(data)
        request = validate_rtc(data["rtc"])
        key = self.jax.random.fold_in(self.jax.random.key(request["trial_seed"]), request["request_index"])
        started = time.perf_counter()
        closed = self.jax.make_jaxpr(self.forward)(self.state, key, batch)
        report = audit_jaxpr(closed)
        lowered = self.jit.lower(self.state, key, batch)
        hlo = str(lowered.compiler_ir(dialect="stablehlo"))
        report.update(audit_stablehlo(hlo))
        report.update(status="MIXED_FORWARD_IR_VERIFIED_NOT_QUALITY_ACCEPTANCE", at=_loader.now(),
            stablehlo_sha256=hashlib.sha256(hlo.encode()).hexdigest(), stablehlo_bytes=len(hlo.encode()),
            source_commit=_loader.COMMIT, implementation_sha256=file_sha(__file__),
            frozen_manifest_sha256=MANIFEST_SHA, trace_lower_seconds=time.perf_counter() - started,
            backend=self.jax.default_backend(), hardware_instruction_trace=False,
            note="IR validates actual mixed graph structure; no robot/quality/latency success claim")
        started = time.perf_counter()
        compiled = lowered.compile()
        self.compile_seconds = time.perf_counter() - started
        _loader.write_json(receipt_dir / "mixed-precision-audit.json", report)
        self.audit, self.compiled = report, compiled
        self.metadata.update(precision_audit_passed=True, status="IR_COMPILED_NOT_QUALITY_ACCEPTED")
        self.rtc.update(precision_audit_passed=True, precision_audit_sha256=_rtc.canonical_sha(report))
        return deepcopy(report)

    def infer_rtc(self, data, *, return_raw=True):
        if self.compiled is None or not self.rtc["precision_audit_passed"]:
            raise RuntimeError("Independent mixed IR must be audited/compiled first")
        started = time.perf_counter()
        request = validate_rtc(data["rtc"])
        batch = self.prepare(data)
        key = self.jax.random.fold_in(self.jax.random.key(request["trial_seed"]), request["request_index"])
        t0 = time.perf_counter()
        raw = self.compiled(self.state, key, batch)
        raw.block_until_ready()
        device_ms = (time.perf_counter() - t0) * 1000
        if str(raw.dtype) != "float32" or raw.shape != (1, 50, 32):
            raise RuntimeError("Raw mixed-policy dtype/shape contract failed")
        raw_host = self.np.asarray(raw)
        if not self.np.isfinite(raw_host).all():
            raise RuntimeError("Non-finite raw latent; no clipping/fallback")
        output = self.unnormalize({"actions": raw_host[0],
            "state": self.np.asarray(batch["observation"]["state"][0])})
        actions = self.np.asarray(output["actions"][:, :7], dtype=self.np.float32)
        if not self.np.isfinite(actions).all():
            raise RuntimeError("Non-finite physical action; no clipping/fallback")
        result = {"actions": actions, "timing": {"device_synchronized_ms": device_ms,
            "total_host_output_ms": (time.perf_counter() - started) * 1000},
            "raw_dtype": str(raw.dtype), "raw_shape": list(raw.shape), "robot_commands": 0,
            "rtc": {**self.rtc, "enabled": request["enabled"],
                "previous_actions_sha256": request["previous_actions_sha256"],
                "trial_index": request["trial_index"], "request_index": request["request_index"],
                "trial_seed": request["trial_seed"], "unknown_error_weights_zero": True,
                "prefix_exactly_overwritten": False, "latent_extra_dimensions_forced_zero": False}}
        if return_raw:
            result["raw_actions32"] = raw_host.copy()
        return result

    infer = infer_rtc  # Full RTC payload only; do not inherit native split-stream RNG.


def create_mixed_adapter(step=5000, *, num_steps=10, seed=42):
    """Return an adapter with .base; call only after the runner owns GPU locks."""
    return MixedPolicy(step=step, num_steps=num_steps, seed=seed)
