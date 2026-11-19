"""Reuse the existing pure governor from this branch's accepted q/v state."""
import numpy as np
from frozen_governor.h50_async import governor_step, physical_actions


def generate_tail(raw,observation_ns,cut_ns,q_rad_m,v_rad_m_s,bounds_rad_m):
    # Import at call time to avoid a cycle with the queue module.
    from .command_pipeline import CubicCurve,NativeGovernorCurve,Plan,Span,label_ns
    to_external=np.asarray([180/np.pi]*6+[1.])
    to_internal=1/to_external
    bounds=np.asarray(bounds_rad_m)*to_external[:,None]
    rows=physical_actions(np.asarray(raw).tolist(),bounds.tolist())
    q=(np.asarray(q_rad_m)*to_external).tolist()
    v=(np.asarray(v_rad_m_s)*to_external).tolist()
    anchor=observation_ns/1e9;when=cut_ns/1e9
    end_ns=observation_ns+label_ns(50);end=end_ns/1e9
    times=[when];positions=[q.copy()];velocities=[v.copy()]
    while end-when>1e-9:
        dt=min(.02,end-when)
        index=min(49.,max(0.,(when+dt-anchor)*18));lo=int(index);hi=min(49,lo+1);fraction=index-lo
        desired=[a+(b-a)*fraction for a,b in zip(rows[lo],rows[hi])]
        q,v=governor_step(q,v,desired,dt,bounds.tolist())
        when+=dt;times.append(when);positions.append(q.copy());velocities.append(v.copy())
    times=np.asarray(times);native_q=np.asarray(positions);native_v=np.asarray(velocities)
    positions=native_q*to_internal;velocities=native_v*to_internal
    # Preserve the actual accepted initial values exactly across unit conversion.
    positions[0]=q_rad_m;velocities[0]=v_rad_m_s
    acceleration=np.diff(velocities,axis=0)/np.diff(times)[:,None]
    curve=CubicCurve(cut_ns,end_ns,tuple(times-times[0]),positions[:-1],velocities[:-1],
                     acceleration,np.zeros_like(acceleration),positions[-1])
    native=NativeGovernorCurve(tuple(times),native_q,native_v,curve)
    return Plan((Span(cut_ns,end_ns,native),))
