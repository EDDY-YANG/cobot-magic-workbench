# Restore one isolated right leader

Copyright (c) 2026 Yang Taige

`tools/restore_right_leader.py` is a narrowly scoped lab recovery utility.
It requires actual on-site confirmation that only the physical right leader
is connected to the selected right CAN branch, with safe physical support and
the other arm isolated according to the manufacturer's procedure.

It reuses the existing writer lock and verified USB Session. It validates
the reviewed SDK source, observes a single-arm feedback rate, queries firmware,
then calls the actual installed `MasterSlaveConfig(0xFA,0,0,0)` exactly once.
Only the firmware-query and FA-configuration packets are allowed.

It never sends enable, disable, reset, homing, joint or gripper motion commands.
The official 0.6.2 `piper_set_master.py` example also configures FA directly;
this utility does not infer that an extra enable/reset is necessary.

A 0x476/0x70 reply confirms a configuration response, not complete teleoperation.
A stationary leader may be silent. New leader control frames and operator
observations must be checked before reconnecting the follower. Restore the
physical OEM pairing with supported, appropriately matched poses and the
manufacturer's power/connection procedure. Never use FA as a broadcast to an
unidentified powered pair, and never equate standby with leader/follower role.

This is not a general hardware installer or a production web route. Private
machine bindings and raw observations stay outside the public repository.
