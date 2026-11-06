# Acceptance checklist

Manual checks before handing Flange Cutter to field users. Tick each box when it passes and note the
date, the machine and who checked.

**Already checked** (ticked) during the build, on 2026-11-06, on the development laptop: Windows 11
Enterprise 10.0.26200 x64, a standard user account **without admin rights**, Python 3.12.10,
Inno Setup 6.7.3, packages built with `build\build_windows.ps1` (version 1.0.0, unsigned). Most of
these were run by script; the evidence is in the commit messages and `docs/DECISIONS.md`.
Everything not ticked is still **open**.

## A. Spec checklist (section 8)

### A1. Per-user installer, no admin rights, no internet

- [x] Setup asks for no admin rights: its manifest requests `asInvoker`; the Inno log shows
  "Administrative install mode: No"; installed to `%LOCALAPPDATA%\Programs\FlangeCutter`; uninstall
  key under HKCU only (silent install as a non-admin user).
- [x] Start menu shortcut "Flange Cutter"; desktop shortcut only when ticked.
- [x] The installed app starts and answers; uninstall removes the program folder, the shortcuts and
  the Apps entry, and keeps `%LOCALAPPDATA%\FlangeCutter`.
- [ ] **By hand, offline:** with Wi-Fi off and the cable out, run the installer with the wizard,
  start Flange Cutter from the Start menu, cut `samples\demo_mold.stl`, download the parts, then
  uninstall from **Settings > Apps**. No admin (UAC) prompt may appear at any step.

### A2. Portable ZIP

- [x] The ZIP has 190 files under `FlangeCutter\`, forward-slash entry names, and a longest path of
  102 characters.
- [x] Extracted into `…\Jiří Novák\Documents\`, the frozen app ran a full flow through its API: open
  the sample, preview, generate, download parts A and B (both watertight, lying on z = 0), ZIP with
  report and settings, repair of a broken file, plain error for a model with holes, quit. It wrote
  nothing into its own folder and listened on 127.0.0.1 only.
- [ ] **By hand, offline:** right-click the ZIP > **Extract All** into Documents, double-click
  `FlangeCutter.exe`, cut the sample, download, quit.

### A3. User name with a space and a non-English character

- [x] Simulated: app folder and data folder under `…\Jiří Novák\…` (data folder set with
  `FLANGECUTTER_HOME`), with the installer, the portable ZIP and the tests.
- [ ] **By hand:** a real Windows account named like `Jiří Novák`: install, start, cut, export,
  uninstall; repeat with the ZIP.

### A4. Upgrade keeps the settings

- [x] 1.0.0 installed, settings saved, then 1.0.1 installed over it **while the app was running**:
  Setup asked the app to quit, replaced it, left one Apps entry, and `settings.json` kept the same
  content (same hash).
- [ ] **By hand:** the same with the wizard (the "Flange Cutter is running" message, OK and Cancel).

### A5. Second start opens the running app

- [x] A second start exits after about 0.5 s with "Flange Cutter is already running: http://…" and
  leaves the lock file alone. After a forced kill, a stale lock file does not stop the next start.
- [ ] **By hand:** double-click the shortcut twice: the second time, the browser opens the running
  app (no second black window stays open).

### A6. Cut the sample lengthwise

- [x] Automated (`tests/test_geometry.py`, `tests/test_ui.py`): `samples\demo_mold.stl`, lengthwise
  cut, 8 × M4: two parts, watertight after STL export and reload, no overlap, no flange in the cavity.
- [ ] **By hand:** in the app, open the sample, **Across Y**, Generate, download both parts.

### A7. PrusaSlicer opens the parts without repair warnings

- [x] Proxy check, automated: every part of every test is watertight after STL export and reload,
  with no degenerate faces.
- [ ] **By hand:** open both parts in **PrusaSlicer 2.9**: no "repaired errors" warning, and each part
  lies on its cut face. Also open the four parts of a two-cut job (Add cut, Generate).

### A8. Print and assemble

- [ ] Cut the sample (or a smaller mold) once, print both parts, assemble them with the listed
  bolts, nuts and washers (washer on both sides, tighten in a cross pattern, do not overtighten).
  The flanges must sit flat on each other and the holes must line up.
- [ ] The same with a cross cut (two cuts, four parts): the bolts near the crossing must not touch
  each other, and every part must be bolted.

## B. Other checks that could not be done on the development laptop

### Windows and security

- [ ] **SmartScreen and Mark of the Web:** download the unsigned installer and ZIP with Edge or
  Chrome on a fresh machine. "Windows protected your PC" > **More info** > **Run anyway** must work
  without admin rights. For the ZIP: Explorer's **Extract All**, and whether files must be unblocked.
- [ ] **Signed build** with the real certificate: `build_windows.ps1 -CertificateFile … ` or
  `-CertificateThumbprint …`. `signtool verify /pa` must pass for the Setup, `FlangeCutter.exe` and
  the installed `unins000.exe`. Then repeat the SmartScreen check.
- [ ] **Managed laptops:** AppLocker, WDAC or Smart App Control, and antivirus products other than
  Defender: may the app run from `%LOCALAPPDATA%\Programs` and from Documents?
- [ ] **Windows 10** (only Windows 11 was tested). Optionally Windows 11 on ARM.
- [ ] A clean machine **without the Visual C++ runtime** installed (the package carries it).

### Installer and app, by hand

- [ ] The installer wizard pages: additional tasks (desktop shortcut), ready page, finish page with
  "Launch Flange Cutter" opening the browser.
- [ ] Uninstall from **Settings > Apps** while the app runs: the message, the app closes, no UAC.
- [ ] Close the black console window with its **X**, then start again: it starts normally.
- [ ] A double-click opens the default browser (Edge or Chrome) on the app (scripts used
  `--no-browser`).
- [ ] **Quit** in the packaged app: the confirmation, the server stops, the page shows "You can close
  this tab now".
- [ ] Downloads in Edge with default settings: part STLs, the ZIP and "Save settings" land in
  Downloads with the right names and no security prompt.
- [ ] Load the `settings.json` from the ZIP: the same cuts come back.
- [ ] Unzip the portable ZIP into a deep folder (e.g. a long OneDrive path).
- [ ] An upgrade across a real change of the bundled libraries (tested only 1.0.0 to 1.0.1 with the
  same libraries).

### Browser, screen and model size

- [ ] Real Edge and Chrome on a laptop with a GPU (the tests use software rendering): turn the view,
  drag the move and rotate handles, snap, click a plane to select it, Shift+click, drag an STL from
  Explorer onto the view, See-through.
- [ ] A 1366 × 768 screen (common field laptop): the panel scrolls, every control can be reached,
  the 2D view stays readable.
- [ ] A large model (about 1 million triangles) in the browser: opening, preview after moving a
  cut, the progress bar during Generate, showing the parts. (Server side, measured here: see
  `docs/ARCHITECTURE.md`, "Performance".)

### Build and CI

- [ ] First **GitHub Actions** run on `windows-latest` after the owner pushes: tests (including the
  UI test) pass, the build passes under PowerShell 7, both artifacts download as plain files.
  Optionally once more with the `CODESIGN_*` secrets set.
- [ ] `build_windows.ps1` under **PowerShell 7** on a developer laptop (tested only in Windows
  PowerShell 5.1).
