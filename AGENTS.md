# Changes and releases

This repository publishes the COBOT MAGIC operator workbench. Keep updates focused on operational functions and recorded evidence.

1. Read `CODEX_HANDOFF.md`, inspect Git status and respect active worktrees. Work on a `codex/` branch. Never overwrite another contributor's changes.
2. Use the 4090 development checkout as the lab authority. Do not edit active release directories. A PC checkout may be used for Windows packaging and publication transport.
3. Run checks relevant to the change. Commit every completed change with a descriptive message, then push its branch. Show the commit/diff to the user. Never claim a local edit is already published.
4. For approved main updates, regenerate the site when required, push `main`, publish `site/` to `gh-pages`, and verify the public HTTP result. Keep a version tag, release notes and SHA-256 for downloadable SDKs.
5. Software publication does not activate a physical controller. Any lab rollout separately checks actual host boot IDs, source hashes, USB identity, service/model instance and current execution owner. Never copy old PASS metadata or clear STOP automatically.
6. Do not commit private profiles, keys, tokens, raw trials, model weights or machine bindings. Only the explicitly approved `site/assets/demo/` recording export is public.
7. Preserve upstream licenses. Do not add the incomplete-license `mobile_aloha_sim` v2 whole-machine meshes or their derived renderings without adequate terms. The public PiPER replay has its separate MIT attribution.
8. Recordings are evidence. Mark failures and unscored outcomes. Kinematic playback of measured joints is not a dynamics/contact validation.
9. Keep one explicit owner for cameras/CAN. Publication work does not start devices or robot motion. Default to one agent.

See `docs/development.md` for commands and `THIRD_PARTY_NOTICES.md` for scope.
