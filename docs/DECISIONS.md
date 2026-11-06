# Decisions

Choices made where the spec (`flange_cutter_prompt.md`) left room, or where the owner changed it.
Newest at the bottom. "Owner" = decided by the project owner; everything else was decided during the build.

## Scope and project

| # | Decision | Why |
|---|---|---|
| 1 | **Several cuts in one session are in scope** (owner, overrides spec §7). Cuts may cross at any angle. | Re-importing a half and cutting again makes crossing cuts collide: the new flange grows past the old cut face, can split old bolt holes, and cannot be repeated exactly on the other half. |
| 2 | **Every cut is fully configurable on its own** (owner): plane, flange side, bolt size, hole size, bolt count or spacing, fastening, all flange parameters. A new cut starts as a copy of the selected cut. | Different cuts have different lengths and loads. |
| 3 | License MIT, copyright holder "SilvestrTk" (owner). | Asked as required by spec §9. |
| 4 | The repository stays local; the owner pushes it and checks the first GitHub workflow run (owner). | Pushing is the owner's call. The workflow runs the same `build/build_windows.ps1` that was tested locally. |
| 5 | The user manual gives no material or print-sheet advice (owner, overrides spec §9). | Owner's choice; print orientation and supports are still covered. |
| 6 | Development Python 3.12.10 installed per user with `winget --scope user`; Inno Setup 6.7.3 the same way. | The development laptop has no admin rights, like the field laptops. |

## Geometry

| # | Decision | Why |
|---|---|---|
| 7 | trimesh's repair steps are called one by one (`update_faces`, `fix_winding`, inversion check). `networkx` is added; `scipy` is dropped. | In trimesh 5, `Trimesh.fix_normals()` silently needs scipy (`body_count`), and `fix_winding` needs networkx. The POC's only scipy use (KD-tree) is replaced by `shapely.STRtree.nearest` and numpy. |
| 8 | Tool clearance (rule 8) tests each bolt's tool disc against the **shadow of the mold beyond both flange faces** (one boolean and one projection per cut) instead of a 3D boolean per bolt against the finished halves. | It is the same cylinder test: a vertical cylinder over the tool height hits the mold exactly when its disc hits that shadow. Flange, holes and pockets never reach past the flange faces. The 3D solid is built once (the POC built it up to 7 times), and preview and Generate place the same bolts. |
| 9 | Only the 2D outline (trimesh `mesh_plane`) shifts the plane by 1 µm when mesh vertices lie exactly on it. Manifold slices and splits at the exact plane. | Vertices on the plane make `mesh_plane` return duplicate edges. Moving the 3D split instead left 1 µm sliver triangles that are no longer watertight after float32 STL export (found by the export-and-reload test). |
| 10 | **Leaning walls:** each flange half also loses the POC guard band found at its own outer face (z = +T and −T). Open guard chains are prolonged past their ends (rims). The two halves are built as one slab plus trimming prisms that start 0.02 mm off the plane. | A straight flange slab next to a wall that leans pokes through the wall into the cavity: 3.5 mm³ in a 3-cut test, and possible below the 25° warning with M5/M6 flanges on 2 mm walls. For square walls the result is identical to the POC. Two overlapping slabs gave coplanar faces that broke after float32 export, hence the trimming prisms. |
| 11 | Bolts are shared over wall pieces by largest remainder, at least 2 per piece where they fit. A count larger than what fits (spacing ≥ washer + 2 mm) is an error that names the maximum. | The POC's rounding could place fewer bolts than asked. Spec §8 asks for a clear error for too many bolts. |
| 12 | Bolt distribution per cut: **count** (spec) or **spacing** (target distance). New parameter "End distance" (default 3 mm) replaces the POC's fixed 3 mm at open flange ends. | Owner asked for full control of hole distribution per cut. |
| 13 | When the wall still blocks tools after moving the bolt line out 6 mm, the layout with the fewest blocked bolts is kept (POC kept the last one). | Moving further out without benefit only makes the flange wider. |
| 14 | Part heights are set from Manifold's double-precision bounding box, not from its float32 mesh output. | Otherwise a tilted part floated 8 µm above z = 0 after export. |
| 15 | Nut-pocket grip = head washer + T + (T − pocket depth) + nut height; the bolt length rule then adds 2 mm and picks the next standard length. | Spec said "compute its own grip". |
| 16 | Parts list names nuts ISO 4032 and washers ISO 7089 (the spec's data sources). Bolt type is left open (any M-size bolt with the listed length). | No head type in the spec. |

## Several cuts

| # | Decision | Why |
|---|---|---|
| 17 | Every plane cuts the whole model. Parts are the non-empty cells of the planes, one STL each, named A, B, C… in order of their side signature. With one cut, A is on the normal side, as the spec says. | Simple, predictable, and identical to the spec for one cut. |
| 18 | Each cut's section and flange come from the original model only. Bolts keep out of a band around every crossing line, half-width `(T_other + r + (T + H)·|cos θ|) / sin θ + 0.5 mm` (r = washer/2 + 0.5, H = tool clearance). | Holes, washers and tool space then stay clear of the other cut's flange slab, also for tilted crossings. |
| 19 | After placing bolts, each bolt's tool space is also checked against the other cuts' flange slabs (warning only). Parallel cuts closer than T₁ + T₂ + 2 mm are an error; closer than T₁ + T₂ + tool clearance a warning. Crossings under 30° give a warning. At most 6 cuts. | Catches what the crossing bands cannot (near-parallel cuts). |
| 20 | One 3D build for all cuts: mold ∪ all flange slabs − all holes and pockets, then split by every plane in turn. | Splitting last keeps every part watertight (spec rule 7 pitfall). |
| 21 | Print orientation of a part with several cut faces: its largest cut face goes down, oriented by the spec's rule for that cut (side A as is, side B turned 180° about the plane x axis). | Matches the spec for one cut; the flange with most contact lies flat. |
