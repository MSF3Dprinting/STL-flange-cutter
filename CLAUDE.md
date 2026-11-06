# Flange Cutter: guide for Claude Code sessions

Local app (Flask + one three.js page) that cuts thin-walled mold STLs with one or more planes and
adds a bolted flange on one face of the wall. Users: MSF field staff on Windows laptops without
admin rights, often offline. Original spec: `flange_cutter_prompt.md`; owner decisions that change
it: `docs/DECISIONS.md` (several crossing cuts per session, every cut fully configurable).

## Commands (Windows, from the project root)

```powershell
py -3.12 -m venv .venv   # or: & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium     # once, for the UI test
.\.venv\Scripts\python.exe -m flangecutter                    # run the app (--no-browser, --port N)
.\.venv\Scripts\python.exe -m pytest tests -q                 # all tests (about 1-2 minutes)
.\.venv\Scripts\python.exe -m pytest tests -q -m "not ui"     # without the browser test
powershell -ExecutionPolicy Bypass -File build\build_windows.ps1   # installer + portable ZIP into build\out
```

**Run the tests before committing.** To refresh the user manual's screenshots in
`docs/screenshots/`, run the UI test with `$env:FLANGECUTTER_SHOTS = "1"` (otherwise it writes
them to a temporary folder).

## Layout

- `flangecutter/geometry/`: `bolts.py` (all ISO data, fastening registry, parameter metadata,
  bolt length rule), `mesh_io.py` (load, repair, STL, ZIP), `cutter.py` (everything geometric).
- `flangecutter/server.py` (thin Flask routes), `session.py` (memory + app folder),
  `__main__.py` (launcher, single instance, logging).
- `flangecutter/static/`: `index.html`, `app.css`, `js/{main,viewer,panel,cutview,api}.js`,
  `vendor/three/` (three.js r187, never edit).
- `tests/`: `test_geometry.py`, `test_api.py`, `test_ui.py`, `demo_mold.py` (POC test mold).
- `reference/`: the proof of concept. **Never change these files.**
- `build/`: PyInstaller spec, Inno Setup script, `build_windows.ps1`. `docs/`: manual, architecture,
  decisions, acceptance checklist.

## Conventions

- Millimeters; viewer Z-up; plane = origin + normal (local +Z) + in-plane x axis (local +X).
- Side A of a cut = where its normal points ("arrow side" in the UI); parts are A, B, C… by side
  signature; with one cut part A is on side A.
- Python: type hints, docstrings, comments where the geometry is not obvious. User-facing text:
  US English, short sentences, every warning has a message and an action.
- Errors the user can fix: raise `geometry.CutError(message, detail)` (HTTP 400). Never let bad
  input crash the server.
- Keep ISO data in `bolts.py` only. Add no dependency without a clear reason (scipy is
  deliberately not used).
- Tests and scripts that start the app set `FLANGECUTTER_HOME` to a temp folder.

## Pitfalls (details in docs/ARCHITECTURE.md)

- Union everything first, **split by the planes last**; never move the 3D split off the plane.
- Snap flange outlines to 0.01 mm before extruding; float32 STL export breaks sub-micron edges.
  Every part must stay watertight after STL export and reload (tests check this).
- trimesh 5 `fix_normals()` needs scipy: call repair steps one by one. Read `model.normals` and
  `model.bounds`, not the trimesh cached properties (slow re-hashing).
- Manifold evaluates lazily; `to_mesh()` returns float32; heights come from `bounding_box()`.
- three.js r187 `TransformControls` is added to the scene through `getHelper()`.
