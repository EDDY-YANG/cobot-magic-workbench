"""Official acados mathematics, platform-specific build plumbing only.

NO_SEND solves are counterfactual evaluations from real feedback. A successful
solve is never an acknowledgement that its output was physically applied.
"""
from pathlib import Path
import ast
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import os
import sys
import time
import types
from contextvars import ContextVar
import numpy as np
from pinned_components import checked

_dll_handles = []
if os.name == 'nt':
    for key in ('V2_CMAKE_BIN', 'V2_TOOLCHAIN_BIN'):
        if os.environ.get(key):
            os.environ['PATH'] = os.environ[key] + os.pathsep + os.environ['PATH']
    for value in (os.environ.get('V2_TOOLCHAIN_BIN'),
                  str(Path(os.environ.get('ACADOS_SOURCE_DIR', '.')) / 'bin')):
        if value and Path(value).is_dir():
            _dll_handles.append(os.add_dll_directory(value))
            os.environ['PATH'] = value + os.pathsep + os.environ['PATH']

import casadi as ca
from acados_template import AcadosModel, AcadosOcp, AcadosOcpSolver

BUILD_RECEIPTS = []
BUILD_ALLOWED = ContextVar('v2_cpu_build_allowed', default=False)

def portable_solver(ocp, json_file):
    acados_root = Path(os.environ['ACADOS_SOURCE_DIR'])
    renderer = acados_root / 'bin' / ('t_renderer.exe' if os.name == 'nt' else 't_renderer')
    if not renderer.is_file():
        raise RuntimeError('Prepare acados t_renderer before starting NO_SEND: ' + str(renderer))
    export = Path(ocp.code_export_directory)
    export.mkdir(parents=True, exist_ok=True)
    symbolic = ca.Function('build_fingerprint', [ocp.model.x, ocp.model.u, ocp.model.p],
        [ocp.model.disc_dyn_expr, ocp.model.cost_y_expr, ocp.model.cost_y_expr_e,
         ocp.model.con_h_expr]).serialize()
    digest = hashlib.sha256(symbolic.encode())
    for a in (ocp.constraints.lbu, ocp.constraints.ubu, ocp.constraints.lh, ocp.constraints.uh):
        digest.update(np.asarray(a, dtype=np.float64).tobytes())
    digest.update((str(ocp.solver_options.tf) + str(ocp.solver_options.N_horizon)
                   + str(sys.platform) + str(ca.__version__)).encode())
    digest.update(checked('frozen/executor.py').read_bytes())
    fingerprint = digest.hexdigest()
    stamp = export / 'v2-build-fingerprint.txt'
    reuse = stamp.is_file() and stamp.read_text().strip() == fingerprint
    if not reuse and not BUILD_ALLOWED.get():
        raise RuntimeError('Solver needs offline prebuild; run scripts/build_solver.py before acquiring the initial observation')
    kwargs = {}
    if os.name == 'nt':
        from acados_template.builders import ocp_get_default_cmake_builder
        builder = ocp_get_default_cmake_builder()
        builder.generator = 'MinGW Makefiles'
        builder.build_dir = str(export / 'build')
        builder.additional_cmake_options = '-DCMAKE_MAKE_PROGRAM=' + str(Path(os.environ['V2_TOOLCHAIN_BIN']) / 'make.exe')
        kwargs['cmake_builder'] = builder
    started = time.perf_counter()
    solver = AcadosOcpSolver(ocp, json_file=json_file, generate=not reuse,
                            build=not reuse, verbose=False, **kwargs)
    stamp.write_text(fingerprint + '\n', encoding='ascii')
    BUILD_RECEIPTS.append({'directory': str(export), 'reused': reuse,
        'construction_seconds': time.perf_counter()-started, 'fingerprint': fingerprint,
        'shared_lib_name': solver.shared_lib_name})
    return solver

def load_official():
    path = checked('frozen/executor.py')
    names = {'MPCConfig', 'PlannerOutput', 'AcadosPlanner', 'CommandStamped', 'ReplayEstimator'}
    nodes = [n for n in ast.parse(path.read_bytes()).body if isinstance(n, ast.ClassDef) and n.name in names]
    if {n.name for n in nodes} != names:
        raise RuntimeError('Pinned official class extraction failed')
    module = types.ModuleType('v2_portable_official_math')
    module.__dict__.update(np=np, ca=ca, dataclass=dataclass, field=field, deque=deque,
        AcadosModel=AcadosModel, AcadosOcp=AcadosOcp, AcadosOcpSolver=portable_solver)
    sys.modules[module.__name__] = module
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), module.__dict__)
    return module

OFFICIAL = load_official()

class ShadowPlanner(OFFICIAL.AcadosPlanner):
    """Official solver retaining actual command history, with latched faults."""
    def __init__(self, cfg, solver_dir, anchor, *, allow_build=False):
        self.reset_count = 0
        self.faulted = False
        permission = BUILD_ALLOWED.set(bool(allow_build))
        try:
            super().__init__(cfg, export_dir=str(Path(solver_dir).resolve()),
                             json_file=str(Path(solver_dir).resolve() / 'ocp.json'))
        finally:
            BUILD_ALLOWED.reset(permission)
        self.reset(np.asarray(anchor, float))

    def reset(self, q0):
        if self.reset_count:
            raise RuntimeError('Midrun state reset is forbidden')
        self.reset_count += 1
        return super().reset(q0)

    def _rate_limit_fallback(self, *args, **kwargs):
        raise RuntimeError('acados failed: no substitute command or automatic reset')

    def evaluate(self, qhat, future, contact=False):
        if self.faulted:
            raise RuntimeError('Planner fault latched')
        qhat = np.asarray(qhat, float)
        future = np.asarray(future, float)
        if qhat.shape != (6,) or future.shape != (15, 6) or not np.isfinite(qhat).all() or not np.isfinite(future).all():
            raise ValueError('Finite six-joint feedback and N15 preview required')
        if np.any(qhat < self.cfg.q_min) or np.any(qhat > self.cfg.q_max):
            raise ValueError('Actual feedback outside limits; never clip sensors')
        actual_history = self.x_aug[6:].copy()
        started = time.perf_counter_ns()
        try:
            out = self.solve(qhat, future, contact_mode=contact)
            predicted = self.solver.get(1, 'x')[:6].copy()
            if self.solver.status != 0 or self._h_bound_stages != tuple(range(self.cfg.N)):
                raise RuntimeError('Failed solve or incomplete stage constraints')
            prev, prev2 = actual_history[:6], actual_history[6:]
            cmd = out.y_cmd
            excess = np.r_[self.cfg.q_min-cmd, cmd-self.cfg.q_max,
                abs(cmd-prev)-self.cfg.dy_max, abs(cmd-2*prev+prev2)-self.cfg.ddy_max,
                abs(cmd-qhat)-self.cfg.tau*self.cfg.v_max*(self.cfg.contact_v_scale if contact else 1.)]
            if not np.isfinite(cmd).all() or excess.max() > 1e-6:
                raise RuntimeError('Solved command fails independent feasibility check')
            info = {'wall_ms': (time.perf_counter_ns()-started)/1e6,
                'solver_ms': float(self.solver.get_stats('time_tot'))*1000,
                'status': int(self.solver.status),
                'constraint_excess_rad': max(0., float(excess.max())),
                'counterfactual_prediction_not_feedback': True,
                'actual_command_history_advanced': False}
            return out, predicted, info
        except Exception:
            self.faulted = True
            raise
        finally:
            # Restore actual command history after counterfactual optimization.
            # This is not a state reset: measured qhat is refreshed normally.
            self.x_aug[:6] = qhat
            self.x_aug[6:] = actual_history

    def record_actual_command(self, q6):
        value = np.asarray(q6, float)
        if value.shape != (6,) or not np.isfinite(value).all():
            raise ValueError('Invalid actual command record')
        self.x_aug[12:] = self.x_aug[6:12].copy()
        self.x_aug[6:12] = value
