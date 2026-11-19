# Native operator UI inspection

The workbench branch adds two presentation assets to the original lab workbench entry point. Existing modules, controllers and form handlers retain their own implementation. The browser theme supplies English display labels and uses native state classes for color. It can also decorate the separate data-review service.

## Inspect a packaged copy on Windows

Extract the complete ZIP. Launch `Start-Review.cmd` with Python 3.9 or later installed. The launcher can reuse a connected inspection service on 8952/8953. Otherwise it starts local inspection listeners on 8962/8963 and attempts read access to the existing 8875/8891 services. If an upstream is absent, the original interface displays its disconnected or unavailable state. Live data, review databases, write tokens, machine configuration and robot recordings are not included in the archive.

The packaged forms, selections, keyboard handlers, charts, 3D assets and dialogs are from the actual application. The inspection server rejects all write requests with HTTP 403, including STOP, movement, saving a review and storage changes. Use the existing operating workstation for an actual stop. Preview tabs use a separate origin and do not share production drafts. Each listener accepts only its local Host header. Both applications can be compared with their source appearance using `/?original=1`.

## Review the same appearance on the original application

The optional `Appearance/` directory is an unpacked Chromium extension for the existing loopback applications. It contains only a manifest, stylesheet and the presentation script, and requests no browser API permissions. Applied to the original application, it uses that application's existing origin, permissions, backend and action handlers. The extension itself never sends an API request. **The original application's buttons are real in this mode.** Inspection does not install or activate this extension.

The package is private workstation delivery. Its static assets and their checksums are listed in `assets.json`; existing robot and three.js licenses are included. Git tracks the styling and inspection tools, not downloaded production assets or evidence. A visual inspection does not establish hardware readiness or task success.


## Original button interfaces on the operator PC

The separate **Workbench Native Buttons** package opens the actual 8875 workbench (or the native 4090 8873 entry) and the original 8891 review app in Microsoft Edge. Extract the package and launch `Open-Original-Workbench.cmd`. It uses the existing lab SSH credentials for any missing loopback forwards. It neither starts an HTTP proxy nor changes the service or CSRF configuration. The launcher performs GET requests to identify the services; robot actions remain attached to the original buttons.

This mode displays **Native controls / 原接口 · 真实操作**. Buttons invoke production actions with all existing restrictions. Edge uses a separate local profile for this appearance version, so browser-only drafts from the ordinary browser are not copied. To retain the ordinary profile's drafts, the same Appearance directory can be loaded as an unpacked extension in that profile instead. The extension has no network API, host proxy or request-modification permissions.
