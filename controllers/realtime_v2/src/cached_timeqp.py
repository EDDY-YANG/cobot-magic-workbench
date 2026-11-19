"""Official TimeQP equations with bounded CSC workspaces and explicit cold iterates.

The frozen optimizer still owns solve(), dt padding, interpolation and float32
output. Only dense matrix assembly / repeated OSQP setup is replaced here.
At target_scale=1 the deployed objective is unchanged. A human target changes
its s_ref preference, upstream normalization and local bounds only as needed
to include that target; solver tolerances, horizon and output grid stay intact.
"""
import time
import numpy as np
import osqp
import scipy.sparse as sp

from .manual_timeqp import validate_target_scale, apply_target_scale


class CachedTimeQP:
    def __init__(self, original_class, cfg, *, reuse_workspace=False, target_scale=1.0):
        self.original = original_class(**cfg)
        self.target_scale = validate_target_scale(target_scale)
        # This immutable workspace owns one target. Keep dt_ref for the output
        # grid unchanged; expand local rate bounds only to include the target.
        apply_target_scale(self.original, self.target_scale)
        if cfg['v_max'] is not None:
            raise ValueError('Only the unchanged deployed v_max=None contract is supported')
        self.reuse_workspace = bool(reuse_workspace)
        self.problem = None
        self.pattern = None
        self.setup_count = self.update_count = 0
        self.last_timing = {}
        self.last_map = None
        self._solve = self.original.solve
        self._reallocate = self.original.re_allocate
        self.original.solve_qp = self.solve_qp
        self.original.solve = self.solve
        self.original.re_allocate = self.re_allocate

    def optimize(self, action_list):
        self.last_timing = {'precompute_ms': 0., 'setup_ms': 0., 'update_ms': 0.,
                            'iterate_reset_ms': 0., 'solve_ms': 0., 'interpolation_ms': 0.}
        self.last_map = None
        return self.original.optimize(action_list)

    def re_allocate(self, *args):
        began = time.perf_counter_ns()
        result = self._reallocate(*args)
        self.last_timing['interpolation_ms'] += (time.perf_counter_ns()-began)/1e6
        return result

    def solve(self, *args):
        result = self._solve(*args)
        self.last_map = (result[1].copy(), result[2].copy())
        return result

    def matrices(self, k):
        ref = self.original
        # Preserve the upstream N-k-1 exactly: native51 has 49 variables,
        # rolling38 has 36. This optimization is not a horizon bug fix.
        size = min(ref.H, ref.N-k-1)
        dp = ref.dp[k:k+size]
        norms = np.linalg.norm(dp, axis=1)
        scale_time = ref.s_ref ** 2 + 1e-6
        scale_acc = np.mean((norms*ref.s_ref) ** 2) + 1e-6
        lambda_time = ref.lambda_time / scale_time
        lambda_acc = ref.lambda_acc / scale_acc
        # Explicit float64 promotion matches upstream scalar operations rather
        # than NumPy's scalar/array float32 promotion. Dot order stays upstream.
        squares = np.sum(dp ** 2, axis=1).astype(np.float64)
        dots = np.asarray([np.dot(dp[i], dp[i+1]) for i in range(size-1)], dtype=np.float64)
        diagonal = np.full(size, 2*lambda_time, dtype=np.float64)
        diagonal[:-1] += 2*lambda_acc*squares[:-1]
        diagonal[1:] += 2*lambda_acc*squares[1:]
        off_diagonal = -2*lambda_acc*dots
        if self.pattern is None:
            indices = np.empty(2*size-1, dtype=np.int32)
            indices[0] = 0; indices[1::2] = np.arange(size-1); indices[2::2] = np.arange(1,size)
            indptr = np.r_[0, np.arange(1,2*size,2)].astype(np.int32)
            self.pattern = (size, indices, indptr)
            self.q = -2*lambda_time*ref.s_ref*np.ones(size)
            self.lower = ref.s_min*np.ones(size); self.upper = ref.s_max*np.ones(size)
            self.A = sp.eye(size, format='csc')
            self.zero = np.zeros(size)
        if self.pattern[0] != size:
            raise ValueError('A TimeQP workspace cannot change shape')
        values = np.empty(2*size-1, dtype=np.float64)
        values[0::2] = diagonal; values[1::2] = off_diagonal
        return values

    def solve_qp(self, k):
        began = time.perf_counter_ns(); values = self.matrices(k)
        self.last_timing['precompute_ms'] += (time.perf_counter_ns()-began)/1e6
        began = time.perf_counter_ns()
        cached = self.problem is not None and self.reuse_workspace
        if not cached:
            size, indices, indptr = self.pattern
            P = sp.csc_matrix((values, indices, indptr), shape=(size,size), copy=True)
            if not self.reuse_workspace:
                # The upstream dense-to-CSC path omits exact zeros. Match its
                # factorization structure even for stationary/orthogonal rows.
                P.eliminate_zeros()
            self.problem = osqp.OSQP()
            self.problem.setup(P=P, q=self.q, A=self.A, l=self.lower, u=self.upper, verbose=False)
            self.initial_rho = self.problem.settings.rho
            self.setup_count += 1
            self.last_timing['setup_ms'] += (time.perf_counter_ns()-began)/1e6
        else:
            self.problem.update(Px=values, q=self.q, l=self.lower, u=self.upper)
            self.update_count += 1
            self.last_timing['update_ms'] += (time.perf_counter_ns()-began)/1e6
            began = time.perf_counter_ns()
            # Do not inherit earlier x/y or adaptive-rho iterates. OSQP matrix
            # scaling still belongs to the first setup and is reported as such.
            self.problem.update_settings(rho=self.initial_rho)
            self.problem.warm_start(x=self.zero, y=self.zero)
            self.last_timing['iterate_reset_ms'] += (time.perf_counter_ns()-began)/1e6
        began = time.perf_counter_ns(); result = self.problem.solve()
        self.last_timing['solve_ms'] += (time.perf_counter_ns()-began)/1e6
        self.last_timing.update(workspace_reused=cached, setup_count=self.setup_count, update_count=self.update_count,
            osqp_iterations=int(result.info.iter), osqp_run_time_ms=float(result.info.run_time)*1000,
            osqp_status=result.info.status, iterate_policy='COLD_ZERO_X_Y_RESET_RHO' if cached else 'FRESH_OFFICIAL_DEFAULT',
            scaling_policy='FIRST_SETUP_RETAINED' if cached else 'FRESH_OFFICIAL_DEFAULT')
        if result.info.status != 'solved':
            raise RuntimeError('OSQP failed: '+result.info.status)
        return np.asarray(result.x, dtype=np.float64)
