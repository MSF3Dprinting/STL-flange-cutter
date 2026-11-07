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
powershell -ExecutionPolicy Bypass -File build\build_windows.ps1   # portable ZIP into build\out (no installer)
```

**Run the tests before committing.** To refresh the user manual's screenshots in
`docs/screenshots/`, run the UI test with `$env:FLANGECUTTER_SHOTS = "1"` (otherwise it writes
them to a temporary folder).

## Layout

- `flangecutter/geometry/`: `bolts.py` (all ISO data, fastening registry, parameter metadata,
  bolt length rule), `mesh_io.py` (load, repair, surface labels, STL clean-up, ZIP), `cutter.py`
  (everything else geometric).
- `flangecutter/server.py` (thin Flask routes), `session.py` (memory + app folder),
  `__main__.py` (launcher, single instance, logging).
- `flangecutter/static/`: `index.html`, `app.css`, `js/{main,viewer,panel,cutview,api}.js`,
  `vendor/three/` (three.js r187, never edit).
- `tests/`: `test_geometry.py`, `test_api.py`, `test_ui.py`, `demo_mold.py` (test molds: POC cup,
  face mask, mask with marks, cone, rounded cup).
- `reference/`: the proof of concept. **Never change these files.**
- `data/*.stl`: real molds made from patient scans, git-ignored. **Never commit, copy or print
  them**; report only aggregate numbers from them.
- `build/`: PyInstaller spec, `build_windows.ps1` (portable ZIP only, decision 46). `docs/`: manual,
  architecture, decisions, acceptance checklist.

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
- No GEOS single-sided buffers (wrong on closed rings; on long wavy open chains the band can start
  W away from the chain): rings use polygon offsets, open chains `_swept_band`.
- Simplify only the band geometry (2 µm, in `_side_band`), never the chains: signs and leans are
  read per piece from the mesh face behind it, and a long simplified piece along a thin wall finds
  the other face and flips the sign.
- Stack the flange layers exactly (outer layers on the core at ±T/2): overlapping or one grid step
  smaller layers left slivers that were not watertight after export.
- A flange face must never lie on a mold face (slivers after export): the guard keeps 0.1 mm
  (`GUARD_GAP`).
- Flange end cuts (`_end_cuts`) trim only their own chain's bands; never prolong guard chains
  globally (across a narrow opening that once erased a whole flange, decision 43).
- `stl_bytes` cleans up and merges flat meshes (a test cone: 23,552 to 1,024 triangles). Tests that
  need a mesh's own triangles write it with `mesh_io.to_trimesh(m).export(file_type="stl")`.
- Validation probes: keep a probe only where the probe itself (not its face center) lies within
  T - 0.3 mm of the cut; judge "face clean" and "past the edge" on added material only (parts minus
  mold); an empty probe set fails the face-clean and coverage checks (the past-the-edge set may be
  empty: not every cut reaches the edge). Add a volume check past the edge: the probes missed an overhang.
- trimesh 5 `fix_normals()` needs scipy: call repair steps one by one. Read `model.normals` and
  `model.bounds`, not the trimesh cached properties (slow re-hashing).
- Manifold evaluates lazily; `to_mesh()` returns float32; heights come from `bounding_box()`.
- three.js r187 `TransformControls` is added to the scene through `getHelper()`.
