# Third-party notices

Original project code and documentation: MIT, Copyright 2026 Yang Taige and Ye Hanzhi / Emerge Robotics. Third-party works retain their own terms.

| Component | Terms and provenance | Distributed scope |
|---|---|---|
| PiPER geometry | MIT, Copyright 2024 RosenYin; agilexrobotics/piper_ros at ac41fcbcdda598f01b51cf6175ed9a24d0dacadc | URDF, meshes, viewer assets and measured-joint render; license beside assets |
| Three.js | MIT, upstream license beside each bundled module | Browser model viewer |
| Realtime-VLA V2 | MIT, Copyright 2026 Dexmal; dexmal/realtime-vla-v2 | Frozen/modified controller components; LICENSE-realtime-vla-v2.txt retained |
| OpenPI | Apache-2.0, Physical Intelligence; Physical-Intelligence/openpi | BF16 frozen source adapters marked as derived/modified; full LICENSE-openpi.txt included |
| CPython 3.14.10 | Python Software Foundation and bundled third-party terms | Official Windows embedded runtime in SDK only; runtime/python/LICENSE.txt |
| Win32-OpenSSH 10.4.0.0p1-Preview | Upstream OpenSSH and bundled notices | Official Windows runtime in SDK only; runtime/openssh/LICENSE.txt and NOTICE.txt |
| Recorded demo export | CC BY 4.0, Emerge Robotics / Yang Taige / Ye Hanzhi | Only site/assets/demo video and measured-joint exports; credit source and note edits |

The full COBOT MAGIC asset in mobile_aloha_sim v2 at f799ae0192c0c2bbd502ec4d9bbec8c2633acd1a declares BSD in package.xml but lacks complete license text. The unrelated master branch has MIT text and does not contain the same model. We do not assume that license applies across branches. The v2 model meshes, GLB and derived renders are excluded; the website links the official source. The PiPER replay is a separately licensed single-arm reference, not a replacement full-machine asset or a calibrated digital twin.

Sources: https://github.com/agilexrobotics/piper_ros ; https://github.com/dexmal/realtime-vla-v2 ; https://github.com/Physical-Intelligence/openpi ; https://www.python.org/downloads/windows/ ; https://github.com/PowerShell/Win32-OpenSSH ; https://creativecommons.org/licenses/by/4.0/

No model weights, training dataset, raw trial archive, private SSH profiles, credentials, runtime acceptance or hardware calibration are licensed or bundled by this release. Institutional names identify the authors' affiliation and do not imply an institutional certification.
