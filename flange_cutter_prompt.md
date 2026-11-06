# Build "Flange Cutter": a local app that cuts thin-walled mold STLs and adds a bolted flange

> **How to use this prompt:** unzip the starter kit into an empty folder. It holds this file and `reference/` with `flange_cut.py` and `test_and_render.py`. Start Claude Code in that folder and say: *"Read flange_cutter_prompt.md and build the app."*
>
> Run Claude Code locally on a Windows laptop for this project, so it can build and test the Windows installer itself. Installing Claude Code on Windows doesn't need admin rights.

## 1. Context

You are building a small tool for MSF (Médecins Sans Frontières) 3D printing users. Most users have no 3D design skills. They work on Windows laptops, often offline, and slice in PrusaSlicer for Prusa printers.

**The problem.** Users print thin-walled molds with walls of 2–3 mm. Large molds must be cut in two, either to fit the printer or so the mold can open. Slicer connectors such as pins, dowels and dovetails don't fit in a 2–3 mm wall.

**The solution is a bolted flange.** Along the cut, the wall is extended in the cut plane into a flange, and bolts pass through the flanges of both halves. The flange grows from **one face of the wall only**, normally the outside. The other face is the mold surface and must stay clean.

**A proof of concept already works.**
- `reference/flange_cut.py` holds the geometry core and a command-line interface.
- `reference/test_and_render.py` runs it on a synthetic 2.5 mm test mold, with a lengthwise cut and a 10° tilted transverse cut, and verifies the results. `reference/poc_preview.png` shows that output.
- Verified results:
  - both halves are watertight;
  - the flange adds no material inside the cavity;
  - bolts are spaced along the wall;
  - the bolt line moves outward automatically when a flaring wall would block the nuts.

Port this algorithm into the app. Don't redesign it. Keep the `reference/` files unchanged.

## 2. Goal

Build a local app with a simple graphical interface where a user can:

1. Import an STL.
2. Place the cut plane with visual handles around the plane, to move and rotate it.
3. Choose the side of the wall that gets the flange.
4. Choose the bolt size and count.
5. Set the flange parameters (thickness and others), starting from sensible defaults.
6. Generate the two halves, review the warnings, and export print-ready STLs.

The app must be easy to maintain and not complex. Documentation and a simple user manual are part of the deliverable.

## 3. Architecture (decided: follow it)

- **Backend:** Python with **Flask**, bound to `127.0.0.1` on a free port. On start, it opens the default browser at the app URL.
- **Frontend:** one page in plain HTML, CSS and JavaScript (ES modules).
  - **three.js** draws the 3D view, using `OrbitControls`, `TransformControls` and `STLLoader`.
  - Copy three.js into `flangecutter/static/vendor/three/` and pin the version. Use no CDN, because the app must work offline.
  - No npm, no bundler, no frontend framework.
- **Geometry libraries:**
  - `manifold3d`: booleans, plane split, slice, extrude.
  - `shapely` 2.x: one-sided 2D offsets and arc length.
  - `trimesh`: STL input/output, repair, and mesh–plane intersection with face indices.
  - `numpy`.
  - Drop `scipy` if it is easy to replace. The POC only uses it for a KD-tree.
- **State:** in memory, for a single user. Nothing is stored except exports and a log file.
- **Python 3.12.** Pin all versions in `requirements.txt`.

**Why this stack:** it reuses the proven Python core. The browser gives the best 3D handles with fewer dependencies than a Qt or VTK desktop app. Plain JavaScript without a build step stays easy to maintain. Ask before switching to another stack.

### Project layout

```
flange-cutter/
├── flangecutter/
│   ├── __init__.py            # __version__
│   ├── __main__.py            # python -m flangecutter: start server, open browser
│   ├── server.py              # Flask app and API routes (keep thin)
│   ├── session.py             # in-memory store of loaded models and results
│   ├── geometry/
│   │   ├── bolts.py           # ISO data, derived defaults, bolt length rules
│   │   ├── mesh_io.py         # load, check, repair, export STL and ZIP
│   │   └── cutter.py          # plane frame, section, flange, bolts, solids, checks
│   └── static/
│       ├── index.html
│       ├── app.css
│       ├── js/main.js         # wiring and app state
│       ├── js/viewer.js       # three.js scene, plane gizmo, overlays, results
│       ├── js/panel.js        # form controls and validation
│       ├── js/cutview.js      # 2D cut view (SVG)
│       ├── js/api.js          # fetch helpers
│       └── vendor/three/      # vendored three.js (pinned)
├── samples/demo_mold.stl      # exported from the POC demo mold
├── reference/                 # POC scripts, unchanged
├── tests/
├── docs/                      # USER_MANUAL.md, ARCHITECTURE.md, DECISIONS.md, ACCEPTANCE.md, screenshots/
├── build/                     # build_windows.ps1, PyInstaller spec, installer.iss (Inno Setup)
├── .github/workflows/windows-build.yml
├── requirements.txt, requirements-dev.txt
└── README.md, CHANGELOG.md, CLAUDE.md, LICENSE
```

### Coordinate conventions

- All units are millimeters.
- The viewer is Z-up (`camera.up.set(0, 0, 1)`), so coordinates match the STL and the slicer.
- The cut plane is a three.js object:
  - origin = its position;
  - normal = its local +Z axis;
  - in-plane x axis = its local +X axis.
- The frontend sends all three. The backend builds the plane frame from them by orthonormalizing, so the 2D cut view matches what the user sees.
- Half **A** is on the side the normal points to. Half **B** is on the other side.

### API (JSON over HTTP)

| Route | Purpose |
|---|---|
| `GET /` | The app page. |
| `GET /api/bolts` | Bolt sizes, fastening types and default parameters, for the UI. |
| `POST /api/model` (multipart `file`) | Load and check the STL. Returns `model_id`, name, triangle count, bounding box, watertight and repaired flags, warnings. |
| `GET /api/model/<id>/mesh.stl` | The checked mesh, for display. |
| `POST /api/preview` | Fast 2D-only preview: section outline, flange outline, bolt positions (2D in the plane and 3D in world space), derived parameters, warnings. Target under 300 ms for 100k triangles. |
| `POST /api/generate` | Full 3D build and checks. Returns `result_id`, report, warnings, parts list. |
| `GET /api/result/<id>/A.stl`, `/B.stl`, `/bundle.zip` | Downloads in print orientation. The ZIP adds `report.txt` and `settings.json`. |
| `POST /api/shutdown` | Stop the server (the Quit button). |

Example payload for preview and generate:

```json
{
  "model_id": "abc123",
  "plane": {"origin": [0, 0, 120], "normal": [0, 1, 0], "x_axis": [1, 0, 0]},
  "side": "outside",
  "bolt": "M4",
  "count": 8,
  "fastening": "through_nut",
  "nut_pocket_half": "B",
  "params": {"thickness": null, "width": null, "wall_gap": 1.0, "edge": 2.0,
             "hole_clearance": 0.2, "overlap": 0.6, "max_wall_angle": 25,
             "tool_clearance": 12}
}
```

- `side` is `outside` or `inside`.
- `fastening` is `through_nut` or `nut_pocket`.
- `null` means "use the automatic value".
- Errors return HTTP 400 as `{"error": "<plain-language message>", "detail": "<technical detail>"}`.

## 4. Geometry rules (port from `reference/flange_cut.py`)

1. **Load.** Load with trimesh (`process=True`) and run a basic repair: merge vertices, remove degenerate and duplicate faces, fix winding and normals. If the mesh is still not watertight, stop and say: *"This model has holes. Repair it first, for example with your slicer's repair function, and try again."* Then convert it to a Manifold, which must have status `NoError`.
2. **Plane frame.** Build an orthonormal frame from origin, normal and x axis. Transform the mesh so the cut plane becomes z = 0.
3. **Section.** `Manifold.slice(0)` gives the outline polygons. `trimesh.intersections.mesh_plane(..., return_faces=True)` gives the same outline as segments, each with the mesh face behind it. Use those face normals.
4. **Cavity side.** The reference point is the centroid of the section's convex hull. A segment faces away from the cavity when its in-plane normal points away from that point.
   - "Outside" uses those segments; "Inside" uses the others.
   - This works for cups, sleeves and rings. For odd shapes, the user switches to the other side.
5. **Flange outline (2D).**
   - Round segment coordinates to 1e-5 mm, then merge the selected segments into chains with `linemerge`.
   - For each chain, combine a band of width W on the flange side with a 0.6 mm overlap strip into the wall.
   - Subtract a guard band (W + 2 mm) on the cavity side of the opposite chains, so nothing enters the cavity.
   - **Pitfall:** GEOS single-sided buffers misbehave on closed rings. Use polygon offsets for closed chains, as `side_band()` in the POC does.
6. **Bolts.**
   - The bolt line is the chain offset by c toward the flange side.
   - Place bolts by arc length: evenly around closed rings. On open chains, keep an end margin of c + washer/2 + 3 mm.
   - When there are several chains, share the count by length, with at least 2 per chain.
   - Suggested default count: one bolt per ~50 mm of bolt line, minimum 4.
   - Checks: spacing at least washer diameter + 2 mm; each washer fits on the flange and does not touch the wall.
7. **Solids.** Extrude the flange outline from −T to +T so it straddles the plane. Union it with the mold, subtract the holes (and nut pockets), and **only then split by the plane**.
   - **Pitfall:** adding a flange onto an already split half creates coplanar faces. After float32 STL export, such halves are no longer watertight. Splitting last avoids this.
8. **Tool clearance.** Beyond each flange face, a cylinder must not intersect its half. The cylinder has radius washer/2 + 0.5 mm and height equal to the tool clearance (default 12 mm).
   - If any cylinder hits, move the bolt line outward in 1 mm steps, up to +6 mm, and tell the user.
   - If it is still blocked, warn for each blocked bolt.
9. **Wall angle.** Warn when the flange-side wall meets the plane more than 25° away from perpendicular, judged on the 95th percentile of segments.
10. **Nut pocket (new, not in the POC).**
    - Shape: a hexagonal pocket, across flats = nut AF + 0.3 mm, depth = nut height + 0.4 mm.
    - Position: from the outer face of the chosen half's flange, coaxial with the hole.
    - Thickness rule: T must be at least nut height + 1.5 mm. If it isn't, increase T and say so.
11. **Export.** Half A stays as is, with the cut face down at z = 0. Half B is rotated 180° about the plane x axis. Both are moved to z = 0. Tests must check watertightness after an STL export and reload.

### ISO data and defaults

| Bolt | Clearance hole (ISO 273, medium) | Washer OD (ISO 7089) | Washer thickness | Nut AF (ISO 4032) | Nut height |
|---|---|---|---|---|---|
| M3 | 3.4 | 7.0 | 0.5 | 5.5 | 2.4 |
| M4 | 4.5 | 9.0 | 0.8 | 7.0 | 3.2 |
| M5 | 5.5 | 10.0 | 1.0 | 8.0 | 4.7 |
| M6 | 6.6 | 12.0 | 1.6 | 10.0 | 5.2 |

Derived defaults, all user-editable:
- Flange thickness per half: T = max(3.5, nominal diameter).
- Bolt line distance from the wall: c = wall gap (1.0) + washer/2.
- Flange width: W = c + washer/2 + edge (2.0).
- Hole diameter = clearance hole + hole clearance (0.2).
- Bolt length = the next standard length that covers the grip plus 2 mm:
  - through-bolt: grip = 2T + 2 washers + nut height;
  - nut pocket: compute its own grip;
  - standard lengths: 8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50;
  - keep these rules in one function in `bolts.py`.

**Performance targets:** preview under 300 ms and generate under 3 s for 100k triangles. STLs up to about 1M triangles must still work; slower is fine, but show progress.

## 5. User interface

**3D view (left, about 70% of the window)**
- The model in light gray, with a transparency toggle.
- The cut plane as a semi-transparent rectangle slightly larger than the model, with the move/rotate gizmo on it.
- On the plane: the section outline in dark gray, the flange preview in amber, bolt markers.
- After Generate: half A in blue, half B in orange, plus an "Explode" slider.
- Optional: Shift+click on the model moves the plane origin to that point.

**Step panel (right).** Each step is disabled until the previous one is done.
1. **Model.**
   - "Open STL" button and drag-and-drop.
   - Shows file name, size (X × Y × Z mm), triangle count, and the check result.
   - Warns when the model is probably in the wrong unit: largest dimension under 10 mm or over 2000 mm.
2. **Cut plane.**
   - Move/Rotate toggle, also on keys W and E.
   - Presets "Across X", "Across Y", "Across Z" (normal along that axis, through the model center), plus "Center on model" and "Reset".
   - Numeric fields for position and rotation, synced both ways with the gizmo.
   - A "Shift along normal" slider.
   - A snap toggle: 1 mm and 5°.
3. **Flange side.** Outside (default) or Inside, with the note *"The mold face on the other side stays clean."*
4. **Bolts and flange.**
   - Bolt size (M3–M6) and count, with a "Suggest" button.
   - Fastening: "Through-bolt and nut", or "Nut pocket" plus the choice of half.
   - Flange thickness and width, showing the automatic value.
   - A collapsible "Advanced" section for the other parameters, and "Reset to defaults".
   - Every field shows its unit, valid range and a tooltip.
5. **Generate and export.**
   - Generate button.
   - Warnings list in plain language, each with what to do.
   - Parts list (bolt length, nuts, washers).
   - Buttons: "Download half A", "Download half B", "Download all (ZIP)", "Save settings", "Load settings".
   - Saved settings include the model file name and its SHA-256, so loading them for a different model shows a warning.

**2D cut view (under the panel).**
- Section in dark gray, flange in amber, holes in white with washer outlines.
- A label on the cavity side.
- Updates with every preview.

**Other behavior**
- The preview updates automatically, debounced by ~200 ms, when the plane or any parameter changes, and at the end of every gizmo drag.
- A status bar shows what is happening and any errors. There is a Quit button.
- Labels use US English, short simple wording, and correct 3D printing terms. Minimum font size 14 px, good contrast, large click targets.
- Disable OrbitControls while the gizmo is being dragged. Check the vendored three.js `TransformControls` API: recent versions add the gizmo to the scene through `getHelper()`.

## 6. Non-functional requirements

- **Local and offline.** No internet at runtime or during installation, no telemetry, server on `127.0.0.1` only.
- **Platforms.** Windows 10/11 laptops. Development also works on Linux and macOS.
- **Run from source** with one command: `python -m flangecutter`.
- **Robustness.** Bad input never crashes the app. Show a plain message and keep the details in a rotating log file in the user's app data folder.
- **Small, readable code.**
  - Aim for about 1,500 lines of Python and 1,200 lines of JavaScript at most, excluding vendored code.
  - Use type hints and docstrings, and comment wherever the geometry is not obvious.
  - Keep all ISO data in one file.
  - Add no dependency without a clear reason.

### Installation without admin rights (local install)

Field users don't have admin rights. Installing, starting, updating and removing the app must never show a Windows admin (UAC) prompt.

- **Bundle everything.** Build with PyInstaller in one-folder mode, which bundles Python and all dependencies.
  - The user needs no Python, no internet and no extra runtime. The app uses the browser that is already installed (Edge or Chrome).
  - Use one-folder mode, not one-file: it starts faster and triggers fewer antivirus false alarms.
  - Don't compress with UPX, for the same reason.
- **Two packages from one build:**
  1. **Per-user installer** `FlangeCutter-Setup-<version>.exe`, made with Inno Setup:
     - `PrivilegesRequired=lowest`, so it never asks for admin rights;
     - installs to `%LOCALAPPDATA%\Programs\FlangeCutter` (`{autopf}` in non-admin mode);
     - adds a Start menu shortcut and an optional desktop shortcut;
     - registers a per-user uninstaller under Settings → Apps;
     - a newer version installs over an older one and keeps the user's settings.
  2. **Portable ZIP**, for laptops where even per-user installers are blocked. The user unzips it anywhere they can write, for example Documents, and double-clicks `FlangeCutter.exe`.
- **What the app may touch:**
  - It writes only to user folders: logs and settings go to `%LOCALAPPDATA%\FlangeCutter\`, and exports go through the browser's normal download. It never writes into the install folder.
  - No services, drivers, scheduled tasks, PATH changes or machine-wide registry keys.
  - It listens on `127.0.0.1` only. Listening on other network interfaces makes Windows Firewall ask for permission, which needs admin rights.
  - It works from paths with spaces and non-English characters, for example `C:\Users\Jiří Novák\Documents`.
- **Starting and stopping.**
  - Double-clicking starts the server and opens the browser. A small console window says "Close this window to quit."
  - If the app is already running, a second start only opens the browser on the running app. Use a small lock file in `%LOCALAPPDATA%\FlangeCutter\` that records the port.
- **Code signing.**
  - The build script takes an optional signing certificate and signs the .exe files and the installer.
  - Without signing, Windows SmartScreen may show "Windows protected your PC". The user can choose "More info → Run anyway" without admin rights.
  - Managed laptops may still block unsigned apps. Document this and plan signing with MSF IT.
- **Build.**
  - `build/build_windows.ps1` produces both packages on Windows.
  - `.github/workflows/windows-build.yml` does the same on `windows-latest` and uploads both packages as artifacts. Install Inno Setup in the workflow if the runner doesn't have it.
  - If you are running on Windows, build and test the packages yourself. If you are running on Linux or macOS, for example in a cloud session, rely on the workflow and list the Windows checks that are still open.

## 7. Out of scope for version 1

- Several cuts in one session. Users re-import a half and cut again.
- Heat-set inserts, a sealing lip or tongue-and-groove on the flange faces, gussets, moving single bolts, curved cuts.
- Embossed labels. MSF avoids embossed text on parts that need cleaning, so identify parts by file name and report instead.

Structure the code so new fastening types and flange features can be added later. For example, keep fastening types in a small registry.

## 8. Tests and acceptance criteria

**Automated tests (pytest)**
- Port the checks from `reference/test_and_render.py`. Use the demo mold with a lengthwise cut (8 × M4) and a 10° tilted transverse cut (6 × M4). Assert:
  - both halves are watertight after STL export and reload, with no degenerate faces;
  - overlap between the halves is under 0.01 mm³;
  - cavity intrusion is within 0.05 mm³ of the plain cut;
  - genus equals the base genus plus the number of holes;
  - the transverse case reports the automatic bolt-line move.
- Every bolt size, both sides and both fastening types run without errors. Nut pocket size and depth are correct.
- Bad inputs give clear errors: plane misses the model, too many bolts, non-watertight mesh, wrong units.
- API tests with Flask's test client.
- UI smoke test with Playwright (headless Chromium): open the demo STL, set the plane with the numeric fields, preview, generate, and download both halves. Use the same run to capture real screenshots for the user manual.

**Manual acceptance.** Write `docs/ACCEPTANCE.md` as a checklist:
- On a Windows account **without admin rights** and with no internet: install with the per-user installer, start the app, cut the sample, export, then uninstall. No admin prompt may appear at any step.
- Repeat with the portable ZIP.
- Repeat on an account whose user name contains a space and a non-English character.
- Install a newer version over the old one and confirm the settings survive.
- Start the app twice and confirm the second start opens the running app.
- Cut the sample lengthwise.
- Open both STLs in PrusaSlicer 2.9 with no repair warnings.
- Print a small test cut and assemble it.

## 9. Documentation

- **`README.md`**, for maintainers: purpose, run from source, build the Windows installer and portable ZIP, code signing, project layout, a short "how it works", how to add a bolt size or fastening type, troubleshooting, license.
- **`docs/USER_MANUAL.md`**, for field users, at most about 6 pages:
  - what the app does and when to use it;
  - how to install, start, update and remove it without admin rights, with both the installer and the portable ZIP;
  - what to do when Windows shows a SmartScreen warning;
  - the five steps, with real screenshots;
  - how to read the 2D cut view;
  - a warnings table: message → meaning → what to do;
  - printing the halves: cut face down so the flange lies flat on the print sheet, no extra supports for the flange, MSF material guidance and the right print sheet for the material;
  - assembly: a washer on both sides, tighten in a cross pattern, don't overtighten thin flanges;
  - FAQ and troubleshooting.
- **`docs/ARCHITECTURE.md`**: data flow (a Mermaid diagram is fine), the API, coordinate conventions, and the geometry steps and pitfalls from section 4.
- **`docs/DECISIONS.md`**: a short log of choices you made where this prompt left room.
- **`CLAUDE.md`**: a short project guide for future Claude Code sessions: commands, layout, conventions, pitfalls, and "run the tests before committing".
- **`CHANGELOG.md`** and **`LICENSE`**. Ask me before choosing a license; MSF publishes similar tools under MIT.

**Style for all user-facing text**
- US English, short and simple sentences, correct 3D printing terms (filament, layers, print sheet).
- Prefer text and tables to images.
- Screenshots must be real screenshots of the app, never mock-ups.
- Keep everything in editable Markdown.

## 10. Way of working

1. Read this file and both reference scripts. Run `reference/test_and_render.py` to see the baseline results.
2. Reply with a short plan by milestone and any blocking questions. For choices that don't block you, decide and log them in `docs/DECISIONS.md`.
3. Work in these milestones. Each one ends with passing tests and a git commit:
   - **M1** Geometry package ported from the POC, with tests.
   - **M2** Flask API, with tests.
   - **M3** 3D viewer, plane gizmo, live preview, 2D cut view.
   - **M4** Generate, results view, downloads, saving and loading settings.
   - **M5** Windows packaging: one-folder build, per-user installer, portable ZIP, signing option, workflow.
   - **M6** Documentation with real screenshots.
4. Prefer simple, readable code over abstractions.

**Done means:**
- all tests pass;
- `python -m flangecutter` runs the app;
- the installer and the portable ZIP build in the workflow;
- the no-admin installation is tested on Windows, or listed as an open check in `docs/ACCEPTANCE.md`;
- all documents in section 9 exist.
