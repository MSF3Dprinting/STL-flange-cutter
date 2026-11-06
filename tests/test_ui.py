"""Browser tests with Playwright (headless Chromium). Skipped when Playwright or its Chromium is not installed:
    pip install -r requirements-dev.txt
    python -m playwright install chromium
The first test also takes the screenshots for the user manual. They go to docs/screenshots/ only when
FLANGECUTTER_SHOTS=1 is set (PowerShell: $env:FLANGECUTTER_SHOTS = "1"), else to a temporary folder.
"""
import itertools
import json
import math
import os
import re
import threading
import zipfile

import pytest

from conftest import ROOT, SAMPLE, reload_stl

sync_api = pytest.importorskip("playwright.sync_api")

from werkzeug.serving import make_server  # noqa: E402

from flangecutter.server import create_app  # noqa: E402
from flangecutter.session import Session  # noqa: E402

pytestmark = pytest.mark.ui
# Software WebGL (SwiftShader): works without a GPU and draws the same pixels on every machine.
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
STATE = "window.flangeCutter.state"

# The preview of cut i is shown and up to date; optionally check its normal and bolt count.
PREVIEW_READY = """([i, normal, bolts]) => {
  const s = window.flangeCutter.state, cut = s.cuts[i], p = cut && s.preview.get(cut.id);
  return !!p && !p.error && cut.shown === cut.version && !s.busy
    && (bolts === null || p.count === bolts)
    && (normal === null || p.frame.normal.every((v, k) => Math.abs(v - normal[k]) < 1e-6));
}"""
# Every cut has an up-to-date preview (with or without an error).
ALL_READY = """() => {
  const s = window.flangeCutter.state;
  return !s.busy && s.cuts.length > 0 && s.cuts.every((c) => s.preview.get(c.id) && c.shown === c.version);
}"""
SCREEN = """([x, y, z]) => {      // a point of the 3D view -> page pixels
  const v = window.flangeCutter.viewer, r = v.renderer.domElement.getBoundingClientRect();
  const p = v.camera.position.clone().set(x, y, z).project(v.camera);
  return [r.left + (p.x + 1) / 2 * r.width, r.top + (1 - p.y) / 2 * r.height];
}"""


@pytest.fixture()
def app_url(tmp_path, monkeypatch):
    """The app in this process on a free port; preferences and logs go to a temporary folder."""
    monkeypatch.setenv("FLANGECUTTER_HOME", str(tmp_path / "home"))
    server = make_server("127.0.0.1", 0, create_app(Session()), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    thread.join(timeout=10)


@pytest.fixture()
def page(app_url):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=CHROMIUM_ARGS)
        except sync_api.Error as exc:
            pytest.skip(f"Chromium for Playwright is not installed: {str(exc).splitlines()[0]}")
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1,
                                accept_downloads=True)
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(app_url)
        assert page.evaluate("!!document.createElement('canvas').getContext('webgl2')"), "no WebGL in Chromium"
        page.wait_for_selector("body[data-ready='1']", timeout=20000)
        yield page
        print("JavaScript errors:", page.errors)      # shown when the test fails
        browser.close()


@pytest.fixture()
def shots(tmp_path):
    """Folder for the screenshots: docs/screenshots with FLANGECUTTER_SHOTS=1, else a temporary folder,
    so a normal test run never changes the files in git."""
    folder = ROOT / "docs" / "screenshots" if os.environ.get("FLANGECUTTER_SHOTS") == "1" else tmp_path / "shots"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def wait_preview(page, i, normal=None, bolts=None):
    page.wait_for_timeout(250)                    # let the debounce start, so the old preview cannot count
    page.wait_for_function(PREVIEW_READY, arg=[i, normal, bolts], timeout=30000)


def wait_all(page):
    page.wait_for_timeout(250)
    page.wait_for_function(ALL_READY, timeout=30000)


def shot(page, path):
    """A screenshot for the user manual: no focus ring, no hover, the 3D view drawn."""
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.mouse.move(800, 990)
    page.wait_for_timeout(400)
    page.screenshot(path=path, animations="disabled")


def scroll_to(page, step_id):
    page.evaluate("(id) => document.getElementById(id).scrollIntoView({block: 'start'})", step_id)


def explode(page, percent):
    page.evaluate("(v) => { const e = document.getElementById('explode'); e.value = v;"
                  " e.dispatchEvent(new Event('input')); }", percent)


def download(page, name, folder):
    with page.expect_download() as info:
        page.get_by_role("link", name=name).click()
    path = folder / info.value.suggested_filename
    info.value.save_as(path)
    return path


def same(a, b):
    """Equal JSON data, numbers within 1e-6 (the page and the server round differently)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) < 1e-6
    return a == b


def move_handle(page, plane):
    """Hover the move arrow of the selected cut (its plane given); returns the mouse position and the
    direction of the arrow on screen, or None."""
    o = page.evaluate(SCREEN, plane["origin"])
    tip = page.evaluate(SCREEN, [plane["origin"][k] + 30 * plane["normal"][k] for k in range(3)])
    n = math.hypot(tip[0] - o[0], tip[1] - o[1]) or 1
    u = ((tip[0] - o[0]) / n, (tip[1] - o[1]) / n)
    for t in range(4, 300, 3):
        page.mouse.move(o[0] + u[0] * t, o[1] + u[1] * t)
        if page.evaluate("window.flangeCutter.viewer.gizmo.axis") == "Z":
            return (o[0] + u[0] * t, o[1] + u[1] * t), u
    return None


def test_cut_generate_download(page, tmp_path, shots):
    expect = sync_api.expect
    assert page.evaluate("document.getElementById('step-cuts').disabled")    # waits for a model

    # Step 1: open the demo mold through the file input
    page.set_input_files("#file-input", str(SAMPLE))
    wait_preview(page, 0)
    expect(page.locator("#m-tris")).to_have_text("32,316")
    expect(page.locator("#m-size")).to_have_text("133.2 × 84 × 240 mm")
    shot(page, shots / "01-model.png")

    # Step 2: a lengthwise cut typed into the number fields: normal +Y through the model center
    # (rotation -90, 0, 180 turns the plane's +Z to +Y and keeps +Z up in the 2D cut view)
    for field, value in (("pos-x", "6.6"), ("pos-y", "0"), ("pos-z", "120"),
                         ("rot-x", "-90"), ("rot-y", "0"), ("rot-z", "180")):
        page.fill(f"#{field}", value)
    wait_preview(page, 0, normal=[0, 1, 0], bolts=8)
    expect(page.locator("#cut-list")).to_contain_text("M4 · 8 bolts · outside")
    expect(page.locator("#cutview-title")).to_have_text("Cut 1, seen from the arrow side")
    shot(page, shots / "02-cut-plane.png")
    scroll_to(page, "step-side")
    shot(page, shots / "03-flange-side.png")
    page.click("#advanced summary")
    scroll_to(page, "step-bolts")
    shot(page, shots / "04-bolts.png")
    page.click("#advanced summary")

    # Step 5: generate, download both parts and the ZIP, save the settings
    page.click("#generate")
    expect(page.locator("#parts-body tr")).to_have_count(2, timeout=60000)
    expect(page.locator("#status-text")).to_have_text("Done: 2 parts. Download them in step 5.")
    expect(page.locator("#joins")).to_contain_text("Cut 1: 8 bolts M4 × 16 mm, 8 nuts, 16 washers")
    shot(page, shots / "05-generate.png")
    for name in "AB":
        data = download(page, f"Download part {name}", tmp_path).read_bytes()
        assert len(data) > 84 and len(data) == 84 + 50 * int.from_bytes(data[80:84], "little")   # binary STL
        assert reload_stl(data).is_watertight
    with zipfile.ZipFile(download(page, "Download all (ZIP)", tmp_path)) as z:
        assert sorted(z.namelist()) == ["demo_mold_part_A.stl", "demo_mold_part_B.stl", "report.txt", "settings.json"]
        in_zip = json.loads(z.read("settings.json"))
    with page.expect_download() as info:
        page.click("#save-settings")
    info.value.save_as(tmp_path / "saved.json")
    saved = json.loads((tmp_path / "saved.json").read_text(encoding="utf-8"))
    assert same(saved, in_zip), (saved, in_zip)
    explode(page, 60)
    page.click("#fit-view")
    shot(page, shots / "06-exploded.png")

    # A second cut across Z: four parts
    page.click("#back-to-edit")
    page.click("#fit-view")
    page.click("#add-cut")
    page.click("[data-preset=z]")
    wait_preview(page, 1, normal=[0, 0, 1])
    wait_preview(page, 0, normal=[0, 1, 0])
    expect(page.locator("#cutview")).to_contain_text("Cut 2, seen from the arrow side")
    expect(page.locator("#step-bolts legend")).to_contain_text("Cut 2")     # steps 3 and 4 name the cut
    shot(page, shots / "07-two-cuts.png")
    page.locator("#cutview").screenshot(path=shots / "08-cut-view.png", animations="disabled")
    page.click("#generate")
    expect(page.locator("#parts-body tr")).to_have_count(4, timeout=60000)
    names = [t.strip() for t in page.locator("#parts-body td:first-child").all_text_contents()]
    assert names == ["A", "B", "C", "D"]
    assert page.locator("#downloads a").count() == 5

    # Settings saved for another model, with one value that is not valid: warnings, and the cuts are used
    saved["model"] = {"name": "other_mold.stl", "sha256": "0" * 64}
    saved["cuts"][0]["bolt"] = "M8"
    (tmp_path / "other.json").write_text(json.dumps(saved), encoding="utf-8")
    page.set_input_files("#settings-input", str(tmp_path / "other.json"))
    wait_preview(page, 0, normal=[0, 1, 0], bolts=8)
    expect(page.locator("#warnings")).to_contain_text('These settings were saved for the model "other_mold.stl".')
    expect(page.locator("#warnings")).to_contain_text("were not valid and were replaced: cut 1 bolt size.")
    assert page.evaluate(f"{STATE}.cuts.length") == 1
    assert page.errors == []


def test_fragile_paths(page):
    """Paths that went wrong before: a click on the gizmo, Generate right after an edit, Suggest on a red
    field, a decimal comma, Add cut twice from the same cut, one cut with an error, Delete cut."""
    expect = sync_api.expect
    page.on("dialog", lambda d: d.accept())                  # "Delete cut 2?"
    page.set_input_files("#file-input", str(SAMPLE))
    wait_preview(page, 0, normal=[0, 0, 1])                  # across the longest side

    # After Generate, a click on the gizmo arrow (with a 2 px shake) keeps the parts; a real drag drops them
    page.click("#generate")
    page.wait_for_function(f"() => {STATE}.result && !{STATE}.busy", timeout=60000)
    page.click("#back-to-edit")
    plane, version = page.evaluate(f"[{STATE}.cuts[0].plane, {STATE}.cuts[0].version]")
    found = move_handle(page, plane)
    assert found, "the move arrow of the cut was not found"
    (x, y), u = found
    page.mouse.down()
    page.mouse.move(x + 2, y + 1)
    page.mouse.up()
    page.wait_for_timeout(300)
    assert page.evaluate(f"[!!{STATE}.result, {STATE}.cuts[0].version, {STATE}.cuts[0].plane]") == [True, version, plane]
    expect(page.locator("#show-parts")).to_be_visible()
    page.mouse.move(x, y)                                    # hover the arrow again: the click ended the last hover
    assert page.evaluate("window.flangeCutter.viewer.gizmo.axis") == "Z"
    page.mouse.down()
    page.mouse.move(x + 30 * u[0], y + 30 * u[1], steps=5)
    page.mouse.up()
    wait_preview(page, 0)
    assert page.evaluate(f"{STATE}.result") is None
    assert page.evaluate(f"{STATE}.cuts[0].plane.origin[2]") != plane["origin"][2]

    # Generate within the 200 ms of the preview after an edit: the preview is up to date afterwards
    page.fill("#count", "10")
    page.click("#generate")
    page.wait_for_function(f"() => {STATE}.result && !{STATE}.busy", timeout=60000)
    expect(page.locator(".cut-row").first).to_contain_text("10 bolts")
    assert page.evaluate(f"{STATE}.cuts.every((c) => c.shown === c.version)")
    assert not page.evaluate("document.getElementById('cutview-svg').classList.contains('stale')")

    # Suggest fixes a red bolt count; a decimal comma is read as a point
    page.click("#back-to-edit")
    page.fill("#count", "")
    expect(page.locator("#count")).to_have_class(re.compile("invalid"))
    expect(page.locator("#generate")).to_be_disabled()
    page.click("#suggest")
    expect(page.locator("#count")).not_to_have_class(re.compile("invalid"))
    expect(page.locator("#count")).to_have_value(str(page.evaluate(f"{STATE}.cuts[0].suggested")))
    expect(page.locator("#generate")).to_be_enabled()
    page.fill("#p-thickness", "4,5")
    wait_preview(page, 0)
    assert page.evaluate(f"{STATE}.preview.get({STATE}.cuts[0].id).derived.thickness") == 4.5
    page.fill("#p-thickness", "")

    # Add cut twice from cut 1: three different planes, no errors
    page.click("#add-cut")
    page.locator(".cut-row").nth(0).click()
    page.click("#add-cut")
    wait_all(page)
    cuts = page.evaluate(f"{STATE}.cuts.map((c) => [c.plane, {STATE}.preview.get(c.id).error])")
    assert [error for _, error in cuts] == [None, None, None]
    for (a, _), (b, _) in itertools.combinations(cuts, 2):
        dot = sum(p * q for p, q in zip(a["normal"], b["normal"]))
        gap = abs(sum(n * (p - q) for n, p, q in zip(a["normal"], a["origin"], b["origin"])))
        assert abs(dot) < 0.99 or gap > 20, (a, b)
    expect(page.locator("#step-side legend")).to_contain_text("Cut 3")

    # Cut 2 misses the model: only cut 2 has an error, the others keep their preview; Generate waits
    page.locator(".cut-row").nth(1).click()
    normal = page.evaluate(f"{STATE}.cuts[1].plane.normal")
    page.fill("#pos-x", "500" if abs(normal[0]) > 0.5 else "6.6")
    page.fill("#pos-y", "500" if abs(normal[1]) > 0.5 else "0")
    page.fill("#pos-z", "800" if abs(normal[2]) > 0.5 else "120")
    wait_all(page)
    assert page.evaluate("""() => { const s = window.flangeCutter.state, v = window.flangeCutter.viewer;
        return s.cuts.map((c, i) => !!s.preview.get(c.id).error === (i === 1) && v.overlays.has(c.id) === (i !== 1)); }""") \
        == [True, True, True]
    expect(page.locator(".cut-row").nth(1)).to_contain_text("Error")
    expect(page.locator("#cut-notes")).to_contain_text("Cut 2 does not go through the model")
    expect(page.locator("#generate")).to_be_disabled()
    expect(page.locator("#generate-why")).to_contain_text("Cut 2 does not go through the model")

    # Delete cut 2: the other cuts are renumbered and their old previews count as out of date at once
    before = page.evaluate(f"{STATE}.cuts.map((c) => c.version)")
    page.click("#delete-cut")
    assert page.evaluate(f"{STATE}.cuts.map((c) => c.version)") == [before[0] + 1, before[2] + 1]
    wait_all(page)
    expect(page.locator("#generate")).to_be_enabled()
    assert page.errors == []
