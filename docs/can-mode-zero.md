# Registered CAN/MOVEJ arms: joint-zero and device selection

The Motion page offers a frozen list of registered CAN/MOVEJ arms. New actions
move_zero_can and move_zero_can_plan carry arms, speed_deg_s and, for execution,
the existing operator declarations. Old single-arm actions and policy/controller
return paths retain their original meaning.

The CLI reads configured CAN interfaces, validates expected USB serials, selects
fresh CAN/MOVEJ feedback, acquires relevant writer leases, and checks the requested
set before target publication. An already-selected disabled or faulted arm blocks
the group; it is not silently omitted or automatically enabled. Other modes,
unavailable interfaces and unregistered devices are not added. The UI lists
targets and exclusions. Six-zero is not calibration or a collision-verified
parking pose. Gripper commands are not included.

Execution uses existing pose-planner speed/acceleration settings, sequential
per-arm execution and a fresh-mode check before each arm. STOP and existing
device/source/storage/feedback guards remain effective. Partial outcomes remain
in the USB session on later failure. This is not synchronized bimanual planning
or proof of swept-volume collision clearance.

Connection provides a static overview with selectable LE/RE/LT/RT and base.
Existing registered follower feedback is displayed independently. Only existing
right-arm takeover/disable actions are wired; unsupported left/teacher transitions,
paired teleoperation, hand guiding and base control remain explicitly unavailable.
Standby is not labeled gravity compensation; disabled is not power-off. Selecting
a device does not submit a hardware action. 3D orbit is a future step.

The overview PNG is a separately configured private installation asset, excluded
from this Git contribution. The entry serves it only when its full path is pinned
in the external installation configuration.

Validation: 17 offline Python tests, 5 device-view JS tests, 4 existing appearance
tests; Python/JS parsing; candidate evaluation on actual read-only web state.
A four-device test fixture verifies selector logic, not four physical devices.
No homing, model loading, enabling, role switching or STOP clearing was performed.
Independent teacher interfaces and full four-arm physical mapping remain
unverified; available operations must reflect that limitation.
