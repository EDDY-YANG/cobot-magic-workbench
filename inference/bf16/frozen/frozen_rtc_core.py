"""Native 32D FP32 H50 RTC with VJP inside every denoising step.

Only seven physical coordinates and 35 known time indices contribute error.
All 32 latent coordinates retain native noise/dynamics; no pad25 projection.
Importing this module does not import JAX or open a device.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time

SCHEMA = 'tiger-fp32-h50-rtc-v1'
HORIZON, PHYSICAL_DIM, LATENT_DIM = 50, 7, 32
PREVIOUS, FREEZE, LABEL_HZ, BETA, NUM_STEPS = 35, 12, 18, 5.0, 10
NATIVE_TRAINING_HZ = 30
F32_MAX = 3.4028234663852886e38


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def implementation_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def prefix_weights():
    """Approved exponential schedule: full0..11, decay12..34, zero35..49."""
    result = []
    for index in range(HORIZON):
        value = 0.0 if index >= PREVIOUS else min(1.0, max(0.0, 1.0 + (FREEZE - 1 - index) / (PREVIOUS - FREEZE + 1)))
        result.append(value * math.expm1(value) / math.expm1(1.0))
    return result


def validate_rtc(value):
    required = {'schema', 'enabled', 'previous_actions', 'previous_valid_steps', 'freeze_steps',
                'execution_label_hz', 'native_training_hz', 'beta', 'trial_index', 'request_index'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Exactly the frozen RTC schema fields are required')
    if value['schema'] != SCHEMA or type(value['enabled']) is not bool:
        raise ValueError('Invalid RTC schema/enabled flag')
    if type(value['freeze_steps']) is not int or value['freeze_steps'] != FREEZE or \
       type(value['execution_label_hz']) is not int or value['execution_label_hz'] != LABEL_HZ or \
       type(value['native_training_hz']) is not int or value['native_training_hz'] != NATIVE_TRAINING_HZ or \
       type(value['beta']) not in (int, float) or value['beta'] != BETA:
        raise ValueError('RTC freeze12/playback18/native-training30/beta5 contract changed')
    for key in ('trial_index', 'request_index'):
        if type(value[key]) is not int or not 0 <= value[key] <= 1_000_000:
            raise ValueError('RTC trial_index/request_index must be zero-based integers in0..1000000')
    previous = value['previous_actions']
    known = PREVIOUS if value['enabled'] else 0
    if type(value['previous_valid_steps']) is not int or value['previous_valid_steps'] != known or \
       not isinstance(previous, list) or len(previous) != known:
        raise ValueError('Bootstrap requires no prefix; RTC requires exactly35 actual accepted reference points')
    if not all(isinstance(row, list) and len(row) == PHYSICAL_DIM and
               all(type(x) in (int, float) and math.isfinite(x) and abs(x) <= F32_MAX for x in row) for row in previous):
        raise ValueError('Accepted prefix must be finite physical rad/m[35,7]')
    result = deepcopy(value)
    result['trial_seed'] = 42 + result['trial_index']
    result['previous_actions_sha256'] = canonical_sha(previous)
    return result


def bootstrap(trial_index=0, request_index=0):
    return {'schema': SCHEMA, 'enabled': False, 'previous_actions': [], 'previous_valid_steps': 0,
            'freeze_steps': FREEZE, 'execution_label_hz': LABEL_HZ, 'native_training_hz': NATIVE_TRAINING_HZ, 'beta': BETA,
            'trial_index': trial_index, 'request_index': request_index}


def guided_velocity(velocity_fn, latent32, denoising_time, previous32, weights32, *, beta=BETA):
    import jax
    import jax.numpy as jnp
    def estimate_clean(value):
        velocity = velocity_fn(value)
        return value - denoising_time * velocity, velocity
    clean, pullback, velocity = jax.vjp(estimate_clean, latent32, has_aux=True)
    # Weights apply ONCE to the cotangent. Unknown time/dimension error is zero.
    correction = pullback((previous32 - clean) * weights32)[0]
    tau = jnp.float32(1.0) - denoising_time
    denominator = jnp.where(tau > 0, denoising_time * tau, jnp.float32(1.0))
    scale = jnp.where(tau <= 0, jnp.float32(beta),
                      jnp.minimum((denoising_time ** 2 + tau ** 2) / denominator, jnp.float32(beta)))
    return velocity - scale * correction


def native32_rtc_sample_actions(model, rng, observation, previous32, weights32, *,
                                num_steps=NUM_STEPS, preprocess=None, attention_mask=None):
    import jax
    import jax.numpy as jnp
    if num_steps != NUM_STEPS or model.action_horizon != HORIZON or model.action_dim != LATENT_DIM:
        raise ValueError('Native RTC requires H50/dim32/10 denoising steps')
    if preprocess is None:
        from openpi.models import model as model_api
        preprocess = lambda obs: model_api.preprocess_observation(None, obs, train=False)
    if attention_mask is None:
        from openpi.models.pi0 import make_attn_mask
        attention_mask = make_attn_mask
    noise = jax.random.normal(rng, (observation.state.shape[0], HORIZON, LATENT_DIM), dtype=jnp.float32)

    def native(_):
        # The same original sampler, exact RNG/noise, no custom zero-weight path.
        return model.sample_actions(rng, observation, num_steps=num_steps, noise=noise)

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
                suffix, s_mask, s_ar, cond = model.embed_suffix(obs, value, jnp.broadcast_to(denoising_time, (value.shape[0],)))
                prefix_mask = jnp.broadcast_to(p_mask[:, None, :], (value.shape[0], suffix.shape[1], p_mask.shape[1]))
                full_mask = jnp.concatenate([prefix_mask, attention_mask(s_mask, s_ar)], axis=-1)
                positions = jnp.sum(p_mask, axis=-1)[:, None] + jnp.cumsum(s_mask, axis=-1) - 1
                (_, output), _ = model.PaliGemma.llm([None, suffix], mask=full_mask, positions=positions,
                                                  kv_cache=cache, adarms_cond=[None, cond])
                return model.action_out_proj(output[:, -model.action_horizon:])
            velocity = guided_velocity(velocity_fn, latent, denoising_time, previous32, weights32)
            return latent + delta * velocity, denoising_time + delta

        def condition(carry):
            return carry[1] >= -delta / jnp.float32(2.0)
        return jax.lax.while_loop(condition, step, (noise, jnp.float32(1.0)))[0]
    return jax.lax.cond(jnp.any(weights32 != 0), guided, native, operand=None)


class RTCFP32Adapter:
    """Share the frozen policy's parameters/transforms; separately audit RTC IR."""
    def __init__(self, base):
        self.base = base
        self.jax, self.jnp, self.np = base.jax, base.jnp, base.np
        self.nnx, self.model_api = base.nnx, base.model_api
        self.state, self.seed = base.state, 42
        self.metadata = deepcopy(base.metadata)
        self.metadata.pop('precision_audit_passed', None)
        self.metadata['rtc_rng_policy'] = 'key=fold_in(key(42+trial_index),request_index)'
        graph, nnx, model_api = base.graph, base.nnx, base.model_api
        def forward(state, rng, batch):
            model = nnx.merge(graph, state)
            obs = model_api.Observation.from_dict(batch['observation'])
            return native32_rtc_sample_actions(model, rng, obs, batch['previous32'], batch['weights32'])
        self.forward, self.jit = forward, self.jax.jit(forward)
        self.compiled, self.audit = None, None
        self.rtc = {'schema': SCHEMA, 'enabled': True, 'precision_audit_passed': False,
                    'implementation_sha256': implementation_sha(), 'precision_audit_sha256': None,
                    'freeze_steps': FREEZE, 'previous_valid_steps': PREVIOUS,
                    'execution_label_hz': LABEL_HZ, 'native_training_hz': NATIVE_TRAINING_HZ, 'beta': BETA}

    def prepare(self, data):
        if not isinstance(data, dict) or set(data) != {'state', 'front', 'right', 'prompt', 'rtc'}:
            raise ValueError('RTC input requires original four observation fields plus rtc')
        request = validate_rtc(data['rtc'])
        observation = self.base.prepare({k: data[k] for k in ('state', 'front', 'right', 'prompt')})
        previous = self.np.zeros((1, HORIZON, LATENT_DIM), self.np.float32)
        weights = self.np.zeros_like(previous)
        if request['enabled']:
            physical = self.np.asarray(request['previous_actions'], dtype=self.np.float32)
            # Same frozen quantile formula/float64 stats as original Normalize.
            stats = self.base.norm['actions']
            normalized = (physical - stats.q01) / (stats.q99 - stats.q01 + 1e-6) * 2.0 - 1.0
            with self.np.errstate(over='ignore', invalid='ignore'):
                normalized32 = normalized.astype(self.np.float32)
            if not self.np.isfinite(normalized32).all():
                raise ValueError('Accepted reference normalization is not representable in float32')
            previous[0, :PREVIOUS, :PHYSICAL_DIM] = normalized32
            weights[0, :, :PHYSICAL_DIM] = self.np.asarray(prefix_weights(), self.np.float32)[:, None]
        return {'observation': observation, 'previous32': self.jnp.asarray(previous), 'weights32': self.jnp.asarray(weights)}

    def audit_and_compile(self, data, receipt_dir):
        # Reuse the original full graph walk/HIGHEST/StableHLO checker on THIS
        # forward graph, not the old native audit or a final-output dtype cast.
        type(self.base).audit_and_compile(self, data, receipt_dir)
        stable = {key: self.audit[key] for key in ('stablehlo_sha256', 'jaxpr_dtype_counts',
                                                  'dot_conv_precision_counts', 'fp16_bf16_nodes', 'source_commit')}
        stable['rtc_implementation_sha256'] = self.rtc['implementation_sha256']
        self.rtc.update(precision_audit_passed=True, precision_audit_sha256=canonical_sha(stable))
        return deepcopy(self.rtc)

    def verify_zero_weight_equivalence(self, data):
        request = {**data, 'rtc': bootstrap()}
        key = self.jax.random.key(42)
        original = self.base.compiled(self.state, key, self.base.prepare(data))
        rtc = self.compiled(self.state, key, self.prepare(request))
        original.block_until_ready()
        rtc.block_until_ready()
        left, right = self.np.asarray(original), self.np.asarray(rtc)
        exact = self.np.array_equal(left, right)
        evidence = {'schema': 'tiger-native32-zero-weight-equivalence-v1', 'pass': bool(exact),
                    'comparison': 'bitwise array_equal raw float32[1,50,32], same key42 and native noise',
                    'absolute_tolerance': 0.0, 'relative_tolerance': 0.0,
                    'max_absolute_difference': float(self.np.max(self.np.abs(left - right))),
                    'native_raw_bytes_sha256': hashlib.sha256(left.tobytes()).hexdigest(),
                    'rtc_raw_bytes_sha256': hashlib.sha256(right.tobytes()).hexdigest(),
                    'implementation_sha256': self.rtc['implementation_sha256']}
        self.zero_weight_arrays = (left, right)
        self.zero_weight_equivalence = evidence
        return evidence

    def infer(self, data):
        # Compatibility endpoint remains native, with the original RNG stream.
        return self.base.infer(data)

    def infer_rtc(self, data):
        if self.compiled is None or self.rtc['precision_audit_passed'] is not True:
            raise RuntimeError('RTC-specific FP32 IR has not been verified')
        started = time.perf_counter()
        request = validate_rtc(data['rtc'])
        batch = self.prepare(data)
        key = self.jax.random.fold_in(self.jax.random.key(request['trial_seed']), request['request_index'])
        device_start = time.perf_counter()
        raw = self.compiled(self.state, key, batch)
        raw.block_until_ready()
        device_ms = (time.perf_counter() - device_start) * 1000
        if str(raw.dtype) != 'float32' or raw.shape != (1, HORIZON, LATENT_DIM):
            raise RuntimeError('RTC raw shape/dtype contract failed')
        unscaled = self.base.unnormalize({'actions': self.np.asarray(raw[0]),
                                        'state': self.np.asarray(batch['observation']['state'][0])})
        actions = self.np.asarray(unscaled['actions'][:, :PHYSICAL_DIM], dtype=self.np.float32)
        if not self.np.isfinite(actions).all():
            raise RuntimeError('RTC produced nonfinite physical actions; not clipped')
        metadata = {**self.rtc, 'enabled': request['enabled'], 'previous_valid_steps': request['previous_valid_steps'],
                    'trial_index': request['trial_index'], 'request_index': request['request_index'],
                    'trial_seed': request['trial_seed'], 'key_seed': request['trial_seed'],
                    'request_key': 'fold_in(key(trial_seed),request_index)',
                    'previous_actions_sha256': request['previous_actions_sha256'],
                    'latent_dimensions': LATENT_DIM, 'guidance_error_physical_dimensions': PHYSICAL_DIM,
                    'unknown_error_weights_zero': True, 'latent_extra_dimensions_forced_zero': False,
                    'prefix_exactly_overwritten': False}
        return {'actions': actions, 'timing': {'device_synchronized_ms': device_ms,
                'total_host_output_ms': (time.perf_counter() - started) * 1000}, 'rtc': metadata}
