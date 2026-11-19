"""Same checked official MPC; measured x is not constrained by command u bounds.

The baseline evaluate() hardcodes an x-versus-u admission check and provides no
hook to override it. This narrow method copy removes only that check; all solver
status, all-stage constraints, finite-input, command-bound feasibility checks,
and actual-command-history preservation remain exactly as in the baseline.
"""
import time
import numpy as np
from portable_backend import ShadowPlanner

class PhysicalPlanner(ShadowPlanner):
    def evaluate(self, qhat, future, contact=False):
        if self.faulted:
            raise RuntimeError('Planner fault latched')
        qhat = np.asarray(qhat, float)
        future = np.asarray(future, float)
        if qhat.shape != (6,) or future.shape != (15, 6) or not np.isfinite(qhat).all() or not np.isfinite(future).all():
            raise ValueError('Finite six-joint feedback and N15 preview required')
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
