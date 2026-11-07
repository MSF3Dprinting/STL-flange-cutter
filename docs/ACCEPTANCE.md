# Acceptance checklist

Manual checks before handing Flange Cutter to field users. Tick each box when it passes and note the
date, the machine and who checked.

**Already checked** (ticked) on the development laptop: Windows 11 Enterprise 10.0.26200 x64, a
standard user account **without admin rights**, Python 3.12.10. Version 1.0.0 was checked on
2026-11-06 with its installer and portable ZIP. Version 1.0.1 was checked on 2026-11-07 with the
portable ZIP, the only package since 1.0.1 (decision 46), built with `build\build_windows.ps1`
(unsigned). Most checks were run by script; the evidence is in the commit messages and
`docs/DECISIONS.md`. Everything not ticked is still **open**.

## A. Spec checklist (section 8)

### A1. No admin rights, no internet

- [x] The build extracts the ZIP and starts that `FlangeCutter.exe` with `--no-browser`: it serves
  its page and the bolt sizes on 127.0.0.1 only and quits cleanly (smoke test of every build).
- [ ] **By hand, offline:** with Wi-Fi off and the cable out, extract the ZIP into Documents
  (**Extract All**), double-click `FlangeCutter.exe`, cut `samples\demo_mold.stl` (copy it from the
  repository first: the ZIP does not contain it), download the parts, quit. No admin (UAC) prompt may appear at any step.
- 1.0.0 only (2026-11-06): the per-user installer asked for no admin rights, installed to
  `%LOCALAPPDATA%\Programs\FlangeCutter` and uninstalled cleanly. There is no installer since 1.0.1.

### A2. Portable ZIP

- [x] 1.0.1: the ZIP has 190 files under `FlangeCutter\`, forward-slash entry names, and a
  longest path of 102 characters.
- [x] 1.0.1: extracted into a folder under `…\Jiří Novák\`, the frozen app ran a full flow through
  its API: open the sample, preview, generate, download parts A and B (both watertight), the ZIP
  with report and settings, quit. It listened on 127.0.0.1 only.
- [ ] **By hand, offline:** right-click the ZIP > **Extract All** into Documents, double-click
  `FlangeCutter.exe`, cut the sample (copied from the repository), download, quit.

### A3. User name with a space and a non-English character

- [x] Simulated: the data folder under `…\Jiří Novák\…` (set with `FLANGECUTTER_HOME`) in the tests
  and in the full-flow run of A2.
- [ ] **By hand:** a real Windows account named like `Jiří Novák`: extract, start, cut, export.

### A4. Update keeps the settings

- [x] Settings and logs live in `%LOCALAPPDATA%\FlangeCutter`, outside the program folder, so
  deleting the old folder and extracting the new ZIP keeps them. Settings files of 1.0.0, which may
  hold the removed "Overlap into wall" value, still load (tests).
- [x] When another version still runs, the new exe says so in its black window and closes it when
  the user presses Enter; without a console it opens the running app as before
  (`test_other_version_is_closed_on_request`).
- [ ] **By hand:** with 1.0.0 running, start the 1.0.1 exe: "Flange Cutter 1.0.0 is still
  running", Enter closes it and 1.0.1 starts; `settings.json` keeps its content. If 1.0.0 was
  installed with Setup, uninstall it from **Settings > Apps** afterwards: the settings stay.
- 1.0.0 only: 1.0.0 installed, settings saved, then a test build numbered 1.0.1 (1.0.0 code)
  installed over it **while the app was running**: Setup asked the app to quit, replaced it, left
  one Apps entry, and `settings.json` kept the same content (same hash).

### A5. Second start opens the running app

- [x] A second start exits after about 0.5 s with "Flange Cutter is already running: http://…" and
  leaves the lock file alone. After a forced kill, a stale lock file does not stop the next start.
- [ ] **By hand:** double-click `FlangeCutter.exe` twice: the second time, the browser opens the
  running app (no second black window stays open).

### A6. Cut the sample lengthwise

- [x] Automated (`tests/test_geometry.py`, `tests/test_ui.py`): `samples\demo_mold.stl`, lengthwise
  cut, 8 × M4: two parts, watertight after STL export and reload, no overlap, no flange in the cavity.
- [ ] **By hand:** in the app, open the sample, **Across Y**, Generate, download both parts.

### A7. PrusaSlicer opens the parts without repair warnings

- [x] Proxy check, automated: the tests reload exported parts and check that they are watertight,
  and the main geometry checks (`check_parts`, the float32, nose and leaning-wall tests) also find
  no degenerate faces. Every part of every real-mold job in A9 is watertight, with no degenerate
  faces.
- [ ] **By hand:** open both parts in **PrusaSlicer 2.9**: no "repaired errors" warning, and each part
  lies on its cut face. Also open the four parts of a two-cut job (Add cut, Generate), and the parts
  of a real face-mask mold cut through the nose with **Inside**.

### A8. Print and assemble

- [ ] Cut the sample (or a smaller mold) once, print both parts, assemble them with the listed
  bolts, nuts and washers (washer on both sides, tighten in a cross pattern, do not overtighten).
  The flanges must sit flat on each other and the holes must line up.
- [ ] The same with a cross cut (two cuts, four parts): the bolts near the crossing must not touch
  each other, and every part must be bolted.
- [ ] One real face-mask mold, cut through the nose with **Inside**: the flange is on the inner
  face along the whole cut, flush and square at the mold's edge, and the outer face is clean.

### A9. Real face-mask molds (1.0.1)

- [x] Four real TFO molds (`data/`, kept out of git: made from patient scans), 120,000 to 241,000
  triangles, 2026-11-07. Ten jobs per mold (through the nose, 25 mm beside it, level at 30 % and
  70 % of the height, oblique 30°, coronal, two crossing cuts, three random planes), each with
  Outside and Inside: 80 jobs, 88 cuts (validation run 8; run 9 on the final code gave the same
  results). Every mold loads as one thin shell
  (outer face, inner face, rim) without warnings.
  - **Parts:** all valid after STL export and reload (watertight, consistent, one body, no
    degenerate faces). Before the export clean-up, 12 of the 80 jobs had an invalid part.
  - **Mold face clean:** no flange material on the protected face at any probe in any cut.
  - **Past the mold's edge:** at most 0.3 % of the probes just outside rim faces lie in flange
    material. A volume check at every flange end found nothing in 85 of the 88 cuts and at most
    14 mm³ (quick test); the exact test of that worst cut leaves 1.6 mm³ past the edge.
  - **Flange joined to the wall:** median 98 % of the flange face covered, 94 % or more in 84 of
    the 88 cuts. Lower: 75 % on one very steep cut (a known limit; the wall-angle warning shows),
    89 % where the mold's edge drops below the upper flange layer (the flange stays flush there),
    and 92 and 93 % on two more cuts.
  - **Preview:** median 0.39 s, at most 0.87 s (busy laptop).
- [ ] **By hand:** the owner's own cuts on these molds, both flange sides: the flange is where it
  should be, the preview is quick enough, and the parts print.

## B. Other checks that could not be done on the development laptop

### Windows and security

- [ ] **SmartScreen and Mark of the Web:** download the unsigned ZIP with Edge or Chrome on a fresh
  machine. Use Explorer's **Extract All**, and check whether `FlangeCutter.exe` must be unblocked.
  "Windows protected your PC" > **More info** > **Run anyway** must work without admin rights.
- [ ] **Signed build** with the real certificate: `build_windows.ps1 -CertificateFile …` or
  `-CertificateThumbprint …`. `signtool verify /pa` must pass for `FlangeCutter.exe`. Then repeat
  the SmartScreen check.
- [ ] **Managed laptops:** AppLocker, WDAC or Smart App Control, and antivirus products other than
  Defender: may the app run from Documents, or from a USB stick?
- [ ] **Windows 10** (only Windows 11 was tested). Optionally Windows 11 on ARM.
- [ ] A clean machine **without the Visual C++ runtime** installed (the package carries it).

### App, by hand

- [ ] Close the black console window with its **X**, then start again: it starts normally.
- [ ] A double-click opens the default browser (Edge or Chrome) on the app (scripts used
  `--no-browser`).
- [ ] **Quit** in the packaged app: the confirmation, the server stops, the page shows "You can close
  this tab now".
- [ ] Downloads in Edge with default settings: part STLs, the ZIP and "Save settings" land in
  Downloads with the right names and no security prompt.
- [ ] Load the `settings.json` from the ZIP: the same cuts come back.
- [ ] Unzip the portable ZIP into a deep folder (e.g. a long OneDrive path).
- [ ] An update across a real change of the bundled libraries (1.0.0 and 1.0.1 bundle the same
  libraries).

### Browser, screen and model size

- [ ] Real Edge and Chrome on a laptop with a GPU (the tests use software rendering): turn the view,
  drag the move and rotate handles, snap, select cuts by clicking their number and their plane (a
  click right next to the selected cut's handles keeps it selected), Shift+click, drag an STL from
  Explorer onto the view, See-through.
- [ ] A 1366 × 768 screen (common field laptop): the panel scrolls, every control can be reached,
  the 2D view stays readable.
- [ ] A large model (about 1 million triangles) in the browser: opening, preview after moving a
  cut, the progress bar during Generate, showing the parts. (Server side, measured here: see
  `docs/ARCHITECTURE.md`, "Performance".)

### Build and CI

- [ ] First **GitHub Actions** run on `windows-latest` after the owner pushes: tests (including the
  UI test) pass, the build passes under PowerShell 7, the ZIP downloads as a plain file. Optionally
  once more with the `CODESIGN_*` secrets set.
- [ ] `build_windows.ps1` under **PowerShell 7** on a developer laptop (tested only in Windows
  PowerShell 5.1).
