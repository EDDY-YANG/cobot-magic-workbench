# DAgger intervention for the AgileX workbench

This extension adds a DAgger page to the existing workbench. It uses the current deployment owner, the original web session token, and the existing workbench state cache. Loading the page reads status. A matching intervention request closes the active policy runner's control pipe. Its existing cancellation and recorder cleanup paths own the stop. The loaded policy service may remain ready for another run.

The on-site system has two shared OEM CAN branches. A leader can send targets directly to its follower. Target agreement with follower feedback cannot authenticate the human source or hold back the first OEM command. The automatic physical handover and automatic human recording start are blocked in this configuration. The page reports elapsed waiting time, joint and gripper differences when fresh, and the actual session state. It cannot arrest a physical arm.

The save action can finalize a native correction recording that carries the exact policy job ID and a dual-arm capture contract. It uses the original path-scoped recording stop and waits for a healthy final receipt. Ending an intervention before a correction exists reports that no human segment was saved.

The device-side source in this branch supports a correction-only capture contract for both followers, the front camera, and the right wrist camera. It preserves raw CAN targets with their source limitations. Deployment to a robot requires the normal source/USB binding and recorder validation. The current shared-bus workbench has no automatic handover or expert-recording trigger.

Open the lab workbench at `/#dagger`, or the management gateway at `/workbench/#dagger`. This branch integrates the page into the lab stack. Machine profiles and runtime bindings are supplied by the installation.

Run the focused offline tests with `python -B -m unittest discover -s tests -p test_dagger_workbench.py`.
