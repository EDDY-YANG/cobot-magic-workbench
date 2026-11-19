# v0.1.2 - Bilingual operator interface

The operational pages now show English labels above smaller Chinese labels. Confirmed readiness uses green, failures use red, and pending checks use amber. The same appearance is available on the separate data-review page.

The page retains the original controls, form values, events and API payloads. The existing DAgger tab is included in the source preview. Its controls require a backend that already supplies the DAgger session state; automatic physical handover remains dependent on that deployment's hardware support.

The public source preview includes connection, capture, motion, deployment, DAgger and the separate review view. The Windows client opens the published offline site and connects to the authorized lab server through its original SSH flow. Downloaded clients require their own configuration and credentials.

A laboratory installation can use `tools/serve_operator_appearance.py` with an external, source-pinned installation file to serve the theme while retaining the existing workbench and review entry points. The original classes handle action requests, origin/token checks, dataset access and recording. Machine paths and pins are kept outside the public package.

Validation covers the portable tests, native module hashes, UI control and request comparisons, local package startup, and the published site. The robot network was unavailable during this release, so current robot boot/USB state, physical motion, hardware stop, and real recording finalization remain unverified.
