# Flange Cutter

A small local app for MSF 3D printing users. It cuts thin-walled mold STLs (2 to 3 mm walls) with
one or more planes and adds a **bolted flange** along every cut, on one face of the wall only, so
the mold surface stays clean. It exports one print-ready STL per part and a hardware list.

- Runs on Windows laptops without admin rights, fully offline, on `127.0.0.1` only.
- Python (Flask + manifold3d + shapely + trimesh) does the geometry; one web page with three.js in
  the user's own browser shows the model and the controls.
- Field users read [docs/USER_MANUAL.md](docs/USER_MANUAL.md). This README is for maintainers.

| Document | What is in it |
|---|---|
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | How to unzip, start, update and use the app (field users). |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Data flow, API, coordinate conventions, geometry steps and pitfalls. |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Choices made during the build and the owner's changes to the spec. |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | Manual acceptance checklist, with what was already checked. |
| [flange_cutter_prompt.md](flange_cutter_prompt.md) | The original specification. |
| [CLAUDE.md](CLAUDE.md) | Short guide for Claude Code sessions. |

## Run from source

Needs Python 3.12 (a per-user install is fine: `winget install Python.Python.3.12 --scope user`).

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m flangecutter
```

The last command starts the server on a free port of `127.0.0.1` and opens the default browser.
Options: `--no-browser`, `--port N`. Logs, preferences and the lock file go to
`%LOCALAPPDATA%\FlangeCutter` (`~/.flangecutter` on Linux and macOS); set `FLANGECUTTER_HOME` to
use another folder. Development also works on Linux and macOS (`python3.12 -m venv .venv`, then
`.venv/bin/python -m flangecutter`).

## Tests

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium   # once, for the UI test
.\.venv\Scripts\python.exe -m pytest tests -q                # everything, about 2 minutes
.\.venv\Scripts\python.exe -m pytest tests -q -m "not ui"    # without the browser test
```

- `tests/test_geometry.py`: the proof-of-concept checks (every part watertight after STL export and
  reload, no overlap, no material in the cavity, genus = plain genus + holes, the automatic bolt-line
  move), every bolt size, side and fastening, nut pocket size and depth, several and crossing cuts,
  bad inputs, and speed on 100k triangles. Thin shells: surface labels for the face mask, the cup
  and a box (not a shell), marks on the face (a raised pad, an engraved line), the cut through the
  nose for both flange sides, the wedge fill on a leaning cone, the flange stopping at the mold's
  edge and flush with a planar edge (also where the cut meets it at a slant), no flange above the
  top of the mold, the fill across the start of a ring, and the rounded-edge fallback (its warning,
  and a clean protected face for both flange sides). Also the STL export's needle merge (it never
  opens the surface). The test molds are in `tests/demo_mold.py`.
- `tests/test_api.py`: the API through Flask's test client, the single-instance lock, and the
  launcher closing another running version when the user asks.
- `tests/test_ui.py`: Playwright with headless Chromium: open, cut, generate, download, two cuts,
  settings, and fragile UI paths (for example which cut a click in the 3D view selects). To refresh
  the manual's screenshots in `docs/screenshots/`:
  `$env:FLANGECUTTER_SHOTS = "1"` before running it.

## Build the Windows package

Needs only the venv above. There is no installer (decision 46): Flange Cutter ships as one
portable ZIP.

```powershell
powershell -ExecutionPolicy Bypass -File build\build_windows.ps1
```

It takes about 2 to 4 minutes and writes `build\out\FlangeCutter-<version>-portable.zip`: the
one-folder build in a `FlangeCutter\` folder. Users extract it anywhere they can write and
double-click `FlangeCutter.exe`. Their settings and logs live in `%LOCALAPPDATA%\FlangeCutter`,
outside the program folder, so an update is: delete the old folder, extract the new one. When an
older version still runs, the new exe offers to close it (`replace_other_version` in
`flangecutter/__main__.py`).

The version comes from `flangecutter/__init__.py`. The script builds with PyInstaller in one-folder
mode (no UPX), signs the exe when it gets a certificate, zips it, then smoke-tests the exe
**unzipped from the ZIP** (start, page, quit). It stops with "BUILD FAILED" and exit code 1 at the
first problem. Options: `-Python`, `-SkipSmokeTest`; run `Get-Help .\build\build_windows.ps1 -Detailed`.

Keep the project path under about 135 characters: Windows without long-path support cannot build in
deeper folders (the script warns).

**Continuous integration.** `.github/workflows/windows-build.yml` runs on `windows-latest` for every
push and pull request: all tests (including the UI test), the same build script, and the portable
ZIP as a downloadable artifact.

## Code signing

Unsigned programs make Windows SmartScreen show "Windows protected your PC" (users can still click
**More info > Run anyway** without admin rights), and managed laptops may block them completely.
Plan a code signing certificate with MSF IT.

The build script signs `FlangeCutter.exe` (before zipping) when it gets a certificate:

```powershell
# .pfx file
.\build\build_windows.ps1 -CertificateFile D:\keys\codesign.pfx -CertificatePassword (Read-Host -AsSecureString "PFX password")
# certificate in the user's store, e.g. on a USB token
.\build\build_windows.ps1 -CertificateThumbprint 0123456789ABCDEF0123456789ABCDEF01234567
```

It uses `signtool.exe` from the Windows SDK (or `-SignTool <path>`), SHA-256 and an RFC 3161
timestamp (`-TimestampUrl`, default `http://timestamp.digicert.com`). In GitHub Actions, add the
repository secrets `CODESIGN_PFX_BASE64` (the .pfx as base64) and `CODESIGN_PFX_PASSWORD`; without
them the exe is built unsigned.

## Project layout

```
flangecutter/
  __init__.py          version
  __main__.py          start: single instance, server on 127.0.0.1, browser, logging
  server.py            Flask routes (thin)
  session.py           in-memory model and results, app folder, preferences
  geometry/
    bolts.py           ISO data, fastening registry, parameters, bolt length rule
    mesh_io.py         load, repair and check STL, label its surfaces; write STL (cleaned up) and ZIP
    cutter.py          sections, flanges, bolts, crossing cuts, 3D build, parts, reports
  static/              index.html, app.css, js/ (main, viewer, panel, cutview, api), vendor/three/
samples/demo_mold.stl  the proof of concept's test mold
reference/             the proof of concept (do not change)
tests/                 pytest: geometry, API, UI (Playwright)
docs/                  user manual, architecture, decisions, acceptance, screenshots
build/                 build_windows.ps1 (portable ZIP), flangecutter.spec (PyInstaller), icon
.github/workflows/     windows-build.yml
```

## How it works

1. The STL is loaded, repaired and checked (trimesh), and turned into a Manifold solid.
2. Every face of a thin-shell mold is labeled once, by the way it faces: outward surfaces (the
   outer side of the wall, with any pads or labels on it), inward surfaces, or rim (the narrow band
   along the mold's edge that joins them); any other model (a solid block, or a mold with a rounded
   edge) keeps the proof of concept's per-segment rule. For every cut, in that cut's plane
   coordinates, the flange is a band of the flange width along the whole cut on the outward surfaces
   (Outside) or the inward ones (Inside), plus 0.6 mm into the wall, and more where the wall leans
   away from the flange by more than 8°. Each half of the flange is two layers, and each layer is
   trimmed at both of its faces: the flange stays 0.1 mm off the other face and ends flush and
   square with the mold's edge at every layer. Bolts are spaced by arc length along a line offset
   from the wall, kept clear of other cuts, and the bolt line moves outward when the wall would
   block a tool.
3. Generate unites the mold with every flange slab, drills the holes and nut pockets, and only then
   splits by every plane. Each piece is checked, named A, B, C…, laid on a cut face and exported.

Details, the API and the pitfalls are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Add a bolt size, a fastening type or a parameter

- **Bolt size**: add one `Bolt(...)` line to `BOLTS` in `flangecutter/geometry/bolts.py` (ISO 273
  hole, ISO 7089 washer, ISO 4032 nut). The page reads the list from `/api/bolts`.
- **Fastening type**: add a `Fastening` to `FASTENINGS` in `bolts.py` (label, help, washers per
  bolt); give it a grip rule in `bolts.bolt_length()`; if it changes the solid, add its cutters in
  `cutter._fastening_cutters()`. The page offers it automatically.
- **Parameter**: add a `Param` to `PARAMS` in `bolts.py` (label, unit, range, default, help, advanced
  or not); use it in `bolts.derive()`. The page builds the field, its range and its tooltip from it.

Add a test for every new option in `tests/test_geometry.py`.

## Troubleshooting

| Problem | What to check |
|---|---|
| The page stays empty or shows "Failed to load module script" | A `.js` file served with the wrong type. `server.py` registers `text/javascript`; check that nothing in front of the app (proxy, antivirus web shield) rewrites it. |
| `ModuleNotFoundError` in the frozen app | Add the module to the PyInstaller spec (`build/flangecutter.spec`) and rebuild. |
| A part is not watertight after export | Run the tests. The STL export (`mesh_io.stl_bytes`) already removes sliver triangles and merges float32 needles, and skips any clean-up step that would open the surface; a part that still comes out open has edges that collapse in float32. See "Pitfalls" in ARCHITECTURE.md (split last, snapping, 1 µm outline shift, 0.1 mm guard gap, exactly stacked flange layers). |
| The second start does not find the running app | Look at `%LOCALAPPDATA%\FlangeCutter\flangecutter.lock` and the log in `logs\`. |
| Anything else | `%LOCALAPPDATA%\FlangeCutter\logs\flangecutter.log` has the details of every error. |

## Known limits and ideas

- A cut always goes through the whole model (no cut limited to one side of another cut).
- Nut pockets in flanges that print upright are not rotated for printing.
- No check against the printer's build volume; the parts table shows each part's size.
- No heat-set inserts, sealing lip, tongue and groove, gussets or embossed labels (spec section 7).
- Very steep walls (almost parallel to the cut, for example 74° median from square on one real
  cut): the two layers per flange half cannot reach the whole flange face (about 75 % covered on
  that cut). The wall-angle warning already says to rotate the cut.
- The mold's edge is checked only at the five heights where the flange layers end (0, ±T/2 and ±T
  from the cut, T = flange thickness). An edge that bulges toward the cut between them can leave a
  small overhang.
- A mold with a rounded edge has no rim to find: it gets the load warning `edge_not_found`, and the
  flange side is chosen piece by piece (the proof of concept's rule, decision 13).
- Within one flange width of the mold's edge there is no wedge fill, only the 0.6 mm strip into the
  wall.
- A cut closer to the mold's edge than the flange thickness gets a shorter or thinner flange there;
  the warnings then say that bolts or washers do not fit, or that a part is not bolted.

## License

MIT, see [LICENSE](LICENSE). three.js (MIT) is included unchanged in
`flangecutter/static/vendor/three/` with its own license file.
