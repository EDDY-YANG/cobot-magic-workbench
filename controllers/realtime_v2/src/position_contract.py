"""One immutable seven-axis command envelope for the isolated V2 runner.

The pinned codec remains the sole owner of SDK saturation/quantization rules.
This contract rejects a declared envelope that the codec would silently narrow,
so native targets, temporal curves, six-joint MPC and final encoding cannot use
different limits. It does not project feedback or change controller state.
"""
from dataclasses import dataclass
import numpy as np

from client_time_queue import require
from pinned_components import command_limiter


@dataclass(frozen=True)
class PositionContract:
    bounds_rad_m: np.ndarray
    codec: object

    def receipt(self):
        return {
            'policy': 'v2_single_effective_position_envelope_v1',
            'bounds_rad_m': self.bounds_rad_m.tolist(),
            'declared_equals_effective': True,
            'sdk_integer_bounds': [list(pair) for pair in self.codec.integer_bounds],
            'feedback_modified': False,
            'dynamics_limits_added': False,
        }


def resolve_position_contract(declared_bounds_rad_m):
    """Resolve already-approved/config-and-controller-intersected bounds.

    Tighter controller limits are valid inputs. Wider gripper declarations must
    be resolved explicitly by the caller, not hidden by the final encoder.
    The codec validates finite shape/order/grid representability and provides
    byte-backed read-only arrays; no duplicate hard-coded bounds live here.
    """
    codec = command_limiter(declared_bounds_rad_m)
    require(np.array_equal(codec.declared_bounds_rad_m, codec.bounds_rad_m),
            'Declared V2 position bounds differ from effective command codec bounds; '
            'resolve the approved/controller intersection before initialization')
    return PositionContract(codec.bounds_rad_m, codec)
