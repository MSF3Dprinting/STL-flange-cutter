# Changelog

All notable changes to Flange Cutter. Versions follow [semantic versioning](https://semver.org/).

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
