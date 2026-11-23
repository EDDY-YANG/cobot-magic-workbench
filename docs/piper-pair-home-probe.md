# PiPER pair-home protocol probe

The installed PiPER SDK can contain a dedicated master-and-slave home request.
This probe tests the actual supplied SDK method without importing its module,
constructing its hardware interface, opening CAN, or sending a robot request.

`ReqMasterArmMoveToHome(2)` uses CAN ID `0x191` with
`01 00 01 00 00 00 00 00`. The SDK documents it as master and slave returning
home together, for firmware V1.7-4 and later. Mode 1 requests master home only;
mode 0 restores master/slave mode. No mode is transmitted by this tool.

This fixed home request is distinct from streaming arbitrary six-joint targets
on IDs `0x155`, `0x156`, `0x157`. A passing probe does not establish that a
powered OEM pair can accept arbitrary VLA targets, that current-position
takeover is bumpless, or that a particular robot's firmware supports the request.

## Run without hardware access

Provide the actual installed interface source file:

```bash
python -B tools/probe_piper_pair_home.py --sdk-interface-file /path/to/piper_sdk/interface/piper_interface_v2.py
python -B -m unittest discover -s tests -p test_piper_pair_home_probe.py
```

The report includes the source SHA-256 and the three checked memory-only packets.
Fixtures in the unit tests are synthetic rejection/isolation tests. Only running
the probe against an installed file tests that file's method.

## Physical-test boundary

Physical execution remains a separate operation. Before sending a home request,
verify arm identities and applicable firmware, the existing unique writer owner,
STOP and recording state, reliable feedback and stationarity, and the swept
workspace of both arms. Retain existing guards. A successful host send is not
proof that either arm moved or that both reached six zero angles.
Do not automatically restore teaching, reset, disable, or retry a motion request
when the outcome is uncertain.

No operator button, live route or firmware upgrade is introduced by this probe.
Lab addresses, machine bindings and raw robot observations are not published.

Source: [official SDK method](https://github.com/agilexrobotics/piper_sdk/blob/0.6.2/piper_sdk/interface/piper_interface_v2.py).
