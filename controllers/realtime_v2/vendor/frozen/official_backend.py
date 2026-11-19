"""Unmodified V2 mathematical classes, real acados, private Windows build.

No robot imports. No OSQP substitute. Caller owns failure/stop decisions.
"""
from pathlib import Path
import ast
from collections import deque
from dataclasses import dataclass, field
import hashlib, json, os, sys, time, types
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
BUILD = Path('D:/2026-10-22-13-47-V2-acados-CPU')
UPSTREAM = PARENT/'upstream/realtime-vla-v2/client/executor.py'
UPSTREAM_SHA = '0868ea33399d6492fb3d154cfdd060214065364aebddd74c9d365ce26d25d121'

os.environ['ACADOS_SOURCE_DIR'] = str(BUILD/'acados')
os.environ['CUDA_VISIBLE_DEVICES'] = ''
sys.dont_write_bytecode=True
os.environ['PATH'] = os.pathsep.join([
    'C:/Program Files/MATLAB/R2024a/bin/win64/cmake/bin',
    str(BUILD/'w64devkit/bin'),str(BUILD/'acados/bin'),os.environ['PATH']])
_dll_handles = [os.add_dll_directory(str(BUILD/p)) for p in ('acados/bin','w64devkit/bin')]
import casadi as ca
from acados_template import AcadosModel, AcadosOcp, AcadosOcpSolver
from acados_template.builders import ocp_get_default_cmake_builder

BUILD_RECEIPTS = []

def windows_solver(ocp, json_file):
    """Build plumbing only: original cost, state, constraints and solver stay intact."""
    if not (BUILD/'acados/bin/t_renderer.exe').is_file():
        raise RuntimeError('offline renderer missing; prepare dependencies before CPU validation')
    builder = ocp_get_default_cmake_builder()
    builder.generator = 'MinGW Makefiles'
    export = Path(ocp.code_export_directory)
    builder.build_dir = str(export/'build')
    builder.additional_cmake_options = '-DCMAKE_MAKE_PROGRAM='+str(BUILD/'w64devkit/bin/make.exe')
    started = time.perf_counter()
    # Full exact configuration, not simply the existence of a DLL, controls reuse.
    symbolic = ca.Function('cpu_build_fingerprint',[ocp.model.x,ocp.model.u,ocp.model.p],
        [ocp.model.disc_dyn_expr,ocp.model.cost_y_expr,ocp.model.con_h_expr]).serialize()
    digest=hashlib.sha256(symbolic.encode())
    for a in (ocp.constraints.lbu,ocp.constraints.ubu,ocp.constraints.lh,ocp.constraints.uh):
        digest.update(np.asarray(a,dtype=np.float64).tobytes())
    digest.update((str(ocp.solver_options.tf)+str(ocp.solver_options.N_horizon)+UPSTREAM_SHA).encode())
    fingerprint=digest.hexdigest()
    stamp = export/'cpu-build-fingerprint.txt'
    reuse = stamp.exists() and stamp.read_text().strip() == fingerprint
    solver = AcadosOcpSolver(ocp,json_file=json_file,cmake_builder=builder,
                            generate=not reuse,build=not reuse,verbose=False)
    stamp.write_text(fingerprint+'\n',encoding='ascii')
    BUILD_RECEIPTS.append({'directory':str(export),'reused':reuse,
        'construction_seconds':time.perf_counter()-started,'fingerprint':fingerprint,
        'shared_lib_name':solver.shared_lib_name})
    return solver

def load_official():
    source = UPSTREAM.read_bytes()
    if hashlib.sha256(source).hexdigest() != UPSTREAM_SHA:
        raise RuntimeError('Pinned V2 source changed')
    names = {'MPCConfig','PlannerOutput','AcadosPlanner','CommandStamped','ReplayEstimator'}
    nodes = [n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name in names]
    assert {n.name for n in nodes} == names
    module = types.ModuleType('pinned_official_v2_acados_cpu')
    module.__dict__.update(np=np,ca=ca,dataclass=dataclass,field=field,deque=deque,
        AcadosModel=AcadosModel,AcadosOcp=AcadosOcp,AcadosOcpSolver=windows_solver)
    sys.modules[module.__name__] = module
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(UPSTREAM),'exec'),module.__dict__)
    return module

OFFICIAL = load_official()

class SolverFailure(RuntimeError):
    pass

class CheckedPlanner(OFFICIAL.AcadosPlanner):
    """Reject a failed solve instead of emitting the author's silent fallback."""
    def __init__(self,cfg,name):
        self.reset_count = 0
        super().__init__(cfg,export_dir=str(BUILD/'generated'/name),
                         json_file=str(BUILD/'generated'/name/'ocp.json'))
        self.records = []
        self.awaiting_command=None
        self.faulted=False

    def reset(self,q0):
        if self.reset_count:
            raise RuntimeError('Mid-run state reset is forbidden')
        self.reset_count += 1
        return super().reset(q0)

    def _rate_limit_fallback(self,*args,**kwargs):
        raise SolverFailure(f'acados status {self.solver.status}; no command emitted')

    def step(self,qhat,future,contact=False):
        if self.faulted:
            raise SolverFailure('planner fault is latched; start a new explicit run')
        if self.awaiting_command is not None:
            raise RuntimeError('previous command has not been acknowledged')
        qhat = np.asarray(qhat,float)
        future = np.asarray(future,float)
        if qhat.shape != (self.nq,) or future.shape != (self.cfg.N,self.nq):
            raise ValueError('exact six-joint state and N15 reference required')
        if not np.isfinite(qhat).all() or not np.isfinite(future).all():
            raise ValueError('nonfinite input; do not clip sensor state')
        if np.any(qhat < self.cfg.q_min-1e-9) or np.any(qhat > self.cfg.q_max+1e-9):
            raise ValueError('feedback outside position bounds')
        if self.initialized:
            yp=self.x_aug[self.nq:2*self.nq].copy()
            yp2=self.x_aug[2*self.nq:].copy()
        else:
            yp=qhat.copy();yp2=qhat.copy()
        start = time.perf_counter_ns()
        try:
            output = self.solve(qhat,future,contact_mode=contact)
        except Exception:
            self.faulted=True
            raise
        wall_ms = (time.perf_counter_ns()-start)/1e6
        if self.solver.status != 0 or self._h_bound_stages != tuple(range(self.cfg.N)):
            self.faulted=True
            raise SolverFailure('solve or stage constraints incomplete')
        cmd = output.y_cmd
        violations = np.r_[self.cfg.q_min-cmd,cmd-self.cfg.q_max,
            abs(cmd-yp)-self.cfg.dy_max,abs(cmd-2*yp+yp2)-self.cfg.ddy_max,
            abs(cmd-qhat)-self.cfg.tau*self.cfg.v_max*(self.cfg.contact_v_scale if contact else 1.)]
        residuals = np.asarray(self.solver.get_residuals(recompute=True))
        if violations.max() > 1e-6 or not np.isfinite(cmd).all():
            self.faulted=True
            raise SolverFailure('returned command fails independent feasibility check')
        item={'wall_ms':wall_ms,'solver_ms':float(self.solver.get_stats('time_tot'))*1000,
              'status':int(self.solver.status),'constraint_excess_rad':max(0.,float(violations.max())),
              'residual_max':float(abs(residuals).max())}
        self.records.append(item)
        self.awaiting_command=cmd.copy()
        return output,self.solver.get(1,'x')[:self.nq].copy(),item

    def acknowledge_command(self,command):
        """Carry actually accepted SDK-grid values, not ideal unquantized values.

        This updates command history, never measured state or initial state.
        In this CPU prototype acknowledgement means a virtual sink, not CAN TX.
        """
        sent=np.asarray(command,float)
        if self.awaiting_command is None or sent.shape!=(self.nq,) or not np.isfinite(sent).all():
            raise ValueError('command acknowledgement mismatch')
        if np.max(abs(sent-self.awaiting_command))>np.deg2rad(.000501):
            raise ValueError('only existing SDK-grid quantization may change solved command')
        self.x_aug[self.nq:2*self.nq]=sent
        self.awaiting_command=None

def provisional_tau():
    doc=json.loads((PARENT/'outputs/provisional-system-model.json').read_text(encoding='utf8'))
    return np.array([a['effective_delay_zero']['tau_s'] for a in doc['axes']])

def config(bounds,*,tau=.15,weight=20.,nq=6):
    return OFFICIAL.MPCConfig(nq=nq,dt=.02,N=15,tau=float(tau),q_min=bounds[:nq,0],
        q_max=bounds[:nq,1],w_track0=10.,w_ddy=float(weight))
