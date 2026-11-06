# Decisions

Choices made where the spec (`flange_cutter_prompt.md`) left room, or where the owner changed it.
"Owner" = decided by the project owner; everything else was decided during the build.
"Review" = changed after an independent design review (three reviewers: geometry, printing and
assembly, simplicity) found a concrete failing case.

## Scope and project

| # | Decision | Why |
|---|---|---|
| 1 | **Several cuts in one session are in scope** (owner, overrides spec §7). Cuts may cross at any angle. | Re-importing a half and cutting again makes crossing cuts collide: the new flange grows past the old cut face, can split old bolt holes, and cannot be repeated exactly on the other half. |
| 2 | **Every cut is fully configurable on its own** (owner): plane, flange side, bolt size, hole size, bolt count or spacing, fastening, all flange parameters. A new cut starts as a copy of the selected cut. | Different cuts have different lengths and loads. |
| 3 | License MIT, copyright holder "SilvestrTk" (owner). | Asked as required by spec §9. |
| 4 | The repository stays local; the owner pushes it and checks the first GitHub workflow run (owner). | Pushing is the owner's call. The workflow runs the same `build/build_windows.ps1` that was tested locally. |
| 5 | The user manual gives no material or print-sheet advice (owner, overrides spec §9). | Owner's choice; print orientation and supports are still covered. |
| 6 | Development Python 3.12.10 and Inno Setup 6.7.3 installed per user with `winget --scope user`. | The development laptop has no admin rights, like the field laptops. |
| 7 | Code size: the Python is above the spec's "about 1,500 lines" (the geometry alone is about 1,500), and the JavaScript is about 1,480 lines instead of about 1,200. | Multi-cut (decision 1) roughly doubles the geometry and adds the cut list, per-cut status and cut placement to the page; the owner accepted this when choosing it. |

## Geometry (one cut)

| # | Decision | Why |
|---|---|---|
| 8 | trimesh's repair steps are called one by one (`update_faces`, `fix_winding`, inversion check). `networkx` is added; `scipy` is dropped. | In trimesh 5, `Trimesh.fix_normals()` silently needs scipy (`body_count`), and `fix_winding` needs networkx. The POC's only scipy use (KD-tree) is replaced by `shapely.STRtree.nearest`. |
| 9 | Tool clearance (rule 8) tests each bolt's tool disc against the **shadow of the mold beyond both flange faces** (one boolean and one projection per cut) instead of a 3D boolean per bolt against the finished halves. | Same test: a vertical cylinder over the tool height hits the mold exactly when its disc hits that shadow (flange, holes and pockets never reach past the flange faces). The 3D solid is built once instead of up to 7 times, and preview and Generate place the same bolts. Verified bolt by bolt against the POC's 3D check. |
| 10 | Only the 2D outline (trimesh `mesh_plane`) shifts the plane by 1 µm when mesh vertices lie exactly on it. Manifold slices and splits at the exact plane. | Vertices on the plane make `mesh_plane` return duplicate edges; moving the 3D split instead left 1 µm slivers that break after float32 STL export. |
| 11 | Flange outlines are **snapped to a 0.01 mm grid** before extruding (review). | Outline vertices made from chain ends lie exactly on the mold surface; Manifold then leaves sub-micron edges that collapse in a float32 STL. The POC itself fails this way at 4 of 5 lengthwise positions; the snap fixed all tested cases (regression tests). |
| 12 | **Leaning walls:** each flange half also loses the POC guard band found at its own outer face (z = ±T); open guard chains are prolonged past their ends (rims). Built as one slab plus trimming prisms that start 0.02 mm off the plane. | A straight flange slab next to a leaning wall pokes through into the cavity (3.5 mm³ in a 3-cut test; possible below the 25° warning with M5/M6 flanges on 2 mm walls). For square walls the result is identical to the POC. |
| 13 | **One cavity reference per connected region of the section** (the centroid of that region's convex hull), not one for the whole section (review). | A plane through two separate wall loops (two arms) put the reference between them and grew flange into both cavities. For one region it is the POC rule. |
| 14 | Bolts are shared over stretches by largest remainder, at least 2 per stretch where they fit. A count larger than what fits (spacing ≥ washer + 2 mm) is an error that names the maximum. | The POC's rounding could place fewer bolts than asked; spec §8 wants a clear error for too many bolts. |
| 15 | Bolt distribution per cut: **count** (spec) or **spacing**. New parameters "End distance" (default 3 mm, the POC's fixed value) and "Hole diameter" (empty = ISO hole + extra hole size). | Owner asked for full control of holes and their distribution per cut. |
| 16 | When the wall still blocks tools after moving the bolt line out 6 mm, the layout with the fewest blocked bolts is kept (POC kept the last). | Moving further out without benefit only widens the flange. |
| 17 | Part heights are set from Manifold's double-precision bounding box, not from its float32 mesh output. | Otherwise a tilted part floated 8 µm above z = 0 after export. |
| 18 | Nut-pocket grip = head washer + T + (T − pocket depth) + nut height; the length rule then adds 2 mm. The nut-pocket thickness rule is applied before the layout. | Spec said "compute its own grip"; the layout must use the final T. |
| 19 | Parts list names nuts ISO 4032 and washers ISO 7089. Head height for clearance is taken as the bolt diameter (socket head, ISO 4762). | The spec's data sources; a hex head is lower, so this is on the safe side. |

## Several cuts

| # | Decision | Why |
|---|---|---|
| 20 | Every plane cuts the whole model. Each cell of the planes is split into its separate bodies; every body is a printed part, named A, B, C… in order of side signature. With one cut, A is on the normal side, as the spec says. | Simple and predictable; one body per file so each can lie flat (review: a 2-body file left one body in mid-air). |
| 21 | Each cut's section and flange come from the original model only. A bolt position is kept only where its hardware and tool space clear every other cut's flange **and the bolt ends, nuts and heads sticking out of it** (review): `max(0, d − (T + H)·cos) − r·sin ≥ T_other + protrusion_other + 0.5`, tested every 0.5 mm along the bolt line. | Clearing only the other flange let bolts of crossing cuts run into each other (shown at 90°). One test covers crossing, tilted and parallel cuts, so there is no special case for parallel planes. |
| 22 | Two cuts must either cross inside the wall at 15° or more (warning below 30°), or keep their flanges apart; decided from where each plane lies relative to the other cut's wall section, never from plane origins (review). Parallel cuts too close for tools between them get no bolts, with a warning that names the other cut. At most 6 cuts. | Origin-based distances gave wrong answers for near-parallel planes. |
| 23 | Preview reports errors **per cut** (HTTP 200); Generate refuses while any cut has an error and lists them (review). | One misplaced cut must not blank the preview of the others. |
| 24 | After Generate: loose flange offcuts (no mold inside) are left out with a warning; parts that are mostly flange, parts or joints held by no bolt or only one bolt, flanges that print in mid-air, and parts that are not watertight after export each give a warning. The report lists which parts each cut bolts together (review). | Infinite planes can cut off pieces nobody asked for; the user must see it before printing. |
| 25 | Print orientation: of the cut faces a part has, the one that leaves the **least overhang** goes on the print sheet (ties: the larger face), oriented by the spec's rule for that cut (side A as is, side B turned 180° about the plane x axis) (review). | "Largest face down" printed cross-cut quarters as arches with the cavity overhanging; with one cut there is only one candidate, so the spec's rule is unchanged. |
| 26 | Not done in version 1 (review suggestions): cuts limited to one side of another cut (T-junctions), hexagon rotation for pockets printed sideways, printer bed-size checks. | Kept the app small; listed as ideas in the README. |

## The page

| # | Decision | Why |
|---|---|---|
| 27 | The first cut goes across the longest side, through the middle of the model's box; if that plane misses the model (two cups side by side), across the next longest side. | Long molds are mostly cut to fit the printer. |
| 28 | **Add cut** copies the selected cut's settings onto a plane turned 90° (or parallel, a quarter of the model away), choosing one that crosses the model and does not lie on another cut. | The first version stacked new cuts on old ones (review). |
| 29 | **Across X/Y/Z** keep the model's +Z up in the 2D view. **Reset** returns a cut to the plane it started with. Number fields accept a decimal comma. | Predictable views; field users often type commas. |
| 30 | The model is tinted by part before Generate (which side of every cut each point is on). | Users see the parts a cut layout makes while they move the cuts. |
| 31 | Any real change after Generate drops the parts and says "Click Generate to make new parts"; a click on a handle without moving it does not. | Downloads must always match what is shown (review). |
| 32 | **Quit**, **Delete cut** and **Use these settings for all cuts** ask for confirmation. | One misclick must not lose work (review). |
| 33 | The server takes a new free port at every start. When it is gone, the page says to start it again and that it opens a new tab. | Spec: "a free port"; a fixed port can be taken by another program. |
| 34 | The UI test writes the manual's screenshots only with `FLANGECUTTER_SHOTS=1`. | A normal test run must not change files in git (review). |

## Packaging

| # | Decision | Why |
|---|---|---|
| 35 | The installer always installs per user to `%LOCALAPPDATA%\Programs\FlangeCutter` and does not offer another folder or an admin mode. | Spec: never an admin prompt; one known place makes support easier. |
| 36 | Setup and the uninstaller ask a running Flange Cutter to quit through `/api/shutdown` (port from the lock file) and fall back to Windows Restart Manager. Uninstall keeps `%LOCALAPPDATA%\FlangeCutter`. | Upgrades while the app runs must work; user settings survive. |
| 37 | Windows 10 or later, x64 (`x64compatible`). The exe carries version information and a generated icon (`build/make_icon.py`). | The libraries are 64-bit; version information helps antivirus tools and support. |
| 38 | `build_windows.ps1` smoke-tests the built exe (start, page, quit) before zipping, and signs before packaging, so the shipped exe is the tested one. Signing in CI only through repository secrets. | A broken or unsigned-by-mistake package must never ship. |
| 39 | The launcher looks for a running app before it loads numpy, trimesh and manifold3d. | A second double-click answers in about 0.5 s instead of several seconds. |
