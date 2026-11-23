# Development and publication

The first public baseline combines management/client source, the current laboratory UI/device source, inference adapters and the versioned controller snapshots. Machine-specific path/address literals in the public copy are replaced by documentation examples; `publication-substitutions.json` records before/after hashes. No live acceptance or boot binding is included.

```bash
git switch -c codex/short-change-name
python -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_ui*.mjs
python tools/build_public_release.py
python tools/build_user_manual.py
git diff --check
git add <reviewed-files>
git commit -m "Describe the behavior change"
git push -u origin HEAD
```

Run only relevant additional checks. For documentation/diagram changes, inspect the rendered page. For device/controller changes, run the applicable offline checks and perform a separately authorized hardware rollout; a GitHub deployment is not hardware acceptance.

After an approved merge into main, publish its exact site tree:

```bash
git push origin main
git subtree split --prefix site -b codex/pages-build
git push origin codex/pages-build:gh-pages
```

Use a fresh temporary split branch per publication. Do not force-push main or overwrite concurrent changes. Verify the GitHub Pages build and public page. Tag a downloadable release only after the Windows ZIP has been freshly extracted, its manifest checked and its bundled Python launcher exercised. The SDK build uses `tools/build_windows_sdk.py --downloads <official-archives> --output <new-directory>`.

For a presentation-only upgrade, the installation may pin two versioned display files under workbench_display, one for /index.html and one for /app.js. The entry verifies each replacement against the installation pins and checks the native pre-theme HTML and JavaScript hashes before startup. The installed backend, SSH bridge and robot source manifest continue to use their existing runtime root. When robot feedback is unavailable the UI displays the disconnected state.

The Windows client includes its own runtime and serves only the public site locally. It opens a private SSH tunnel after the operator chooses Connect, validates the management protocol, and proxies the server's operational UI. It does not mirror or replace the server's control code and never retries a robot POST.

The `lab_stack`, `controllers`, and `inference` folders preserve the research implementation. Their installation-specific environments, paths, calibration and source identity checks must be configured and rebound on each new lab installation; they are not a universal plug-and-play robot installer. See the source snapshot manifests for the exact imported revisions. Model weights remain separately provisioned.
