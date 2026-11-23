# COBOT MAGIC Workbench v0.1.3

The Connection page puts the whole-machine top view between four arm cards. Each card shows its own feedback and offers controls for that arm. The right execution arm (RE) has an explicitly scoped PC takeover request and a separately gated motor-disable request. Selecting a card or the base opens device details. Unknown or unbound devices are labeled individually.

- Use the RE card to request CAN / MOVEJ. The additional takeover and six-motor disable buttons in the Connection toolbar have been removed. The firmware status and verification of the six drives are displayed separately. The RE diagnostics and maintenance controls identify their target. The existing right-arm gate also controls the card during submission and when shared feedback is invalid.
- In Motion tests, group joint-zero freezes the set of registered arms already reporting CAN / MOVEJ. It checks identity, feedback, the six enable flags, STOP and ownership, and executes each accepted arm in sequence. The gripper stays at its current opening. The pose executor rechecks the selected arm and performs its normal CAN mode handshake before sending the zero trajectory.
- Active DAgger intervention retains its own job binding. Software stop is accessible from the header and the deployment page. Recording has a separate stop-and-save action. The global STOP banner contains the latch-clear action.
- Explicit USB reconnect rechecks the original mounted device and idle ownership. Right-side shared-feedback faults and new CAN errors are surfaced in the workbench.
- The public interface is a read-only preview. It can show the four-arm top view by reading the pinned AgileX assembly from its upstream source. Without geometry or WebGL it shows a labeled schematic. The Windows SDK includes the offline workbench and the authorized SSH connection flow.

**Hardware scope.** LE is the left execution arm, RE the right execution arm, LT and RT the left and right teaching arms. Paired teleoperation and hand guiding remain unavailable in the arm cards. The verified live check for this update covers the web interface and readback. Four-arm physical switching, homing and motion were not exercised during release verification.

[Release files](https://github.com/EDDY-YANG/cobot-magic-workbench/releases/tag/v0.1.3) · [中文版本说明](release-v0.1.3.zh.md) · [Scope and upstream attribution](../THIRD_PARTY_NOTICES.md).
