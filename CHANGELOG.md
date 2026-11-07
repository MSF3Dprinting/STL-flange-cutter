# Changelog

All notable changes to Flange Cutter. Versions follow [semantic versioning](https://semver.org/).

## 1.0.1 (2026-11-07)

Fixes from the first test with real face-mask (TFO) molds.

- The flange now follows one face of the mold along the whole cut. Before, on wavy profiles (for
  example a cut through the nose) it could come in pieces, partly on the wrong face.
- The flange stops square and flush at the mold's edge instead of wrapping around it, also where
  the edge meets the cut at a slant. Where the edge is closer to the cut than the flange is thick,
  the flange gets thinner or shorter there instead of standing past the edge.
- Where the wall meets the cut at a slant, the flange is now joined to the wall over its whole
  thickness (no wedge-shaped gap), except within one flange width of the mold's edge and on very
  steep walls, where the wall-angle warning shows. It still never touches the mold face.
- Marks on the mold face (a raised pad or label, an engraved line) no longer confuse which face
  gets the flange, and two cavities joined in one mold each keep their mold face clean.
- A mold whose edge is rounded now gives a warning: Flange Cutter cannot tell its two faces apart
  there and chooses the flange side piece by piece. Check the flange in the preview.
- Fixed a flange that could disappear completely on cuts near a narrow opening of the mold.
- Fixed parts that were sometimes not watertight, or had sliver triangles, after export.
- 3D view: a click near the handles of the selected cut no longer selects another cut, so
  rotating one cut can no longer move another. A click on a cut's number selects that cut.
- The "Overlap into wall" setting is gone. The flange always reaches 0.6 mm into the wall (the
  old default), and deeper where the wall leans away from it.
- **No installer any more.** Flange Cutter comes as a portable ZIP only: extract it and
  double-click `FlangeCutter.exe`. If you installed 1.0.0 with Setup, uninstall it once from
  **Settings > Apps**; your settings stay.
- When an older version is still running, the new version offers to close it first. Before, a
  second start silently opened the old version.

## 1.0.0 (2026-11-06)

First release.

- Open an STL, check it and repair small problems (merged vertices, degenerate or duplicate
  faces, face winding, inside-out bodies). Warns when the model is probably in the wrong unit.
- Place one or more cut planes (up to 6, at any angle, crossing allowed) with move and rotate
  handles, presets and numeric fields.
- Every cut has its own settings: flange side (outside or inside), bolt size M3 to M6, bolt count
  or spacing, through-bolt and nut or nut pocket, flange thickness, width, hole diameter and
  advanced values.
- Live 2D and 3D preview of each cut's section, flange and bolts; per-cut errors and warnings in
  plain language, each with what to do.
- Generate builds all cuts at once. Every part is watertight after STL export, lies on a cut
  face, and is named A, B, C… Bolts of crossing cuts stay clear of each other's flanges and
  hardware.
- Downloads: one STL per part, or a ZIP with all parts, `report.txt` (parts, joins, hardware
  list, warnings) and `settings.json`. Save and load settings.
- Runs offline on 127.0.0.1 only; per-user installer and portable ZIP for Windows that never ask
  for admin rights.
