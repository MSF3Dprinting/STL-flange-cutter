"""API tests with Flask's test client, plus the launcher's single-instance check."""
import io
import json
import threading
import zipfile

import pytest
import trimesh
from werkzeug.serving import make_server

from conftest import SAMPLE, reload_stl
from flangecutter import __main__ as launcher
from flangecutter import __version__
from flangecutter.server import create_app
from flangecutter.session import Session, app_dir

LENGTHWISE = {"plane": {"origin": [0, 0, 120], "normal": [0, 1, 0], "x_axis": [1, 0, 0]},
              "side": "outside", "bolt": "M4", "count": 8, "fastening": "through_nut", "nut_pocket_half": "B",
              "params": {"thickness": None, "width": None, "wall_gap": 1.0, "edge": 2.0, "hole_clearance": 0.2,
                         "overlap": 0.6, "max_wall_angle": 25, "tool_clearance": 12}}
# pins with the fit values off their defaults, and bolt values that must not leak into a pin cut
PINS = dict(LENGTHWISE, bolt="pin", params=dict(LENGTHWISE["params"], pin_diameter=8, pin_hole_clearance=0.3,
                                                pin_preload=0.3, hole_diameter=5.0, hole_clearance=1.0))
ACROSS = {"plane": {"origin": [0, 0, 120], "normal": [0, 0, 1], "x_axis": [1, 0, 0]}, "count": 6, "bolt": "pin"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Preferences, lock file and logs go to a temporary folder."""
    monkeypatch.setenv("FLANGECUTTER_HOME", str(tmp_path / "Jiří Novák" / "FlangeCutter"))
    return tmp_path


@pytest.fixture()
def client():
    return create_app(Session()).test_client()


def upload(client, data: bytes, name: str = "demo_mold.stl"):
    return client.post("/api/model", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


@pytest.fixture()
def model_id(client, sample_bytes):
    r = upload(client, sample_bytes)
    assert r.status_code == 200, r.json
    return r.json["model_id"]


def strict_json(text: str):
    return json.loads(text, parse_constant=lambda c: pytest.fail(f"{c} is not valid JSON"))


def test_ping_and_catalog(client):
    assert client.get("/api/ping").json["app"] == "flangecutter"
    cat = client.get("/api/bolts").json
    assert [b["name"] for b in cat["bolts"]] == ["M3", "M4", "M5", "M6"]
    assert {f["key"] for f in cat["fastenings"]} == {"through_nut", "nut_pocket"}
    assert {p["key"] for p in cat["params"]} >= {"thickness", "width", "hole_diameter", "tool_clearance"}
    assert cat["bolts"][1]["auto"] == {"thickness": 4.0, "bolt_line": 5.5, "width": 12.0, "hole_diameter": 4.7}
    pin = cat["pin"]
    assert (pin["key"], pin["label"], pin["spares"], pin["suggest_pitch"]) == ("pin", "Pin", 2, 35)
    assert pin["auto"] == {"thickness": 6.0, "bolt_line": 7.0, "width": 15.0, "hole_diameter": 8.1}
    params = {p["key"]: p for p in cat["params"]}
    d = params["pin_diameter"]
    assert (d["min"], d["max"], d["default"], d["kinds"], d["advanced"]) == (8, 12, 8.0, ["pin"], False)
    assert params["pin_hole_clearance"]["label"] == "Extra pin hole size"
    assert params["hole_clearance"]["kinds"] == ["bolt"] and params["thickness"]["kinds"] == ["bolt", "pin"]
    assert params["wall_gap"]["pin_label"] == "Gap pin head to wall" and cat["count"]["suggest_pitch"] == 50


def test_upload_reports_the_model(client, sample_bytes):
    info = upload(client, sample_bytes).json
    assert info["triangles"] == 32316 and info["watertight"] and not info["repaired"]
    assert info["bbox"]["size"] == pytest.approx([133.2, 84.0, 240.0], abs=0.01)
    assert len(info["sha256"]) == 64 and info["warnings"] == []


def test_full_flow_preview_generate_download(client, model_id):
    mesh = client.get(f"/api/model/{model_id}/mesh.stl")
    assert mesh.status_code == 200 and len(mesh.data) > 1_000_000

    pv = client.post("/api/preview", json={"model_id": model_id, **LENGTHWISE})   # the spec's payload form
    assert pv.status_code == 200
    cut = strict_json(pv.data)["cuts"][0]
    assert cut["error"] is None and cut["count"] == 8 and len(cut["bolts"][0]["xyz"]) == 3

    gen = client.post("/api/generate", json={"model_id": model_id, "cuts": [LENGTHWISE]})
    assert gen.status_code == 200, gen.json
    out = strict_json(gen.data)
    assert [p["name"] for p in out["parts"]] == ["A", "B"]
    assert out["hardware"][0] == {"item": "Bolt", "spec": "M4 × 16 mm", "qty": 8}
    assert out["cuts"][0]["joins"] == [{"parts": ["A", "B"], "bolts": 8}]
    assert out["pins"] == [] == out["fit_tests"] and out["cuts"][0]["kind"] == "bolt"
    r = client.get(f"/api/result/{out['result_id']}/pins/1.stl")
    assert r.status_code == 400 and "There are no pins for cut 1" in r.json["error"]
    for part in out["parts"]:
        r = client.get(part["url"])
        assert r.status_code == 200 and part["file"] in r.headers["Content-Disposition"]
        assert reload_stl(r.data).is_watertight
        assert len(part["to_world"]) == 16
    z = zipfile.ZipFile(io.BytesIO(client.get(out["zip"]).data))
    names = set(z.namelist())
    assert names == {"demo_mold_part_A.stl", "demo_mold_part_B.stl", "report.txt", "settings.json"}
    settings = json.loads(z.read("settings.json"))
    assert settings["format"] == 1 and settings["model"]["name"] == "demo_mold.stl" and len(settings["cuts"]) == 1
    assert "Part A: demo_mold_part_A.stl" in z.read("report.txt").decode("utf-8")
    # the saved settings load back as a request
    again = client.post("/api/preview", json={"model_id": model_id, "cuts": settings["cuts"]})
    assert again.json["cuts"][0]["count"] == 8


def test_three_parallel_slices_give_valid_json(client, model_id):
    cuts = [{"plane": {"origin": [0, 0, z], "normal": [0, 0, 1]}, "count": 6} for z in (80, 160)]
    out = strict_json(client.post("/api/generate", json={"model_id": model_id, "cuts": cuts}).data)
    assert len(out["parts"]) == 3 and all(len(p["explode"]) == 3 for p in out["parts"])


@pytest.mark.parametrize("payload, status, message", [
    ({"model_id": "nope", **LENGTHWISE}, 400, "no longer open"),
    ({"model_id": "MID", "cuts": []}, 400, "at least one cut"),
    ({"model_id": "MID", "cuts": [dict(LENGTHWISE, bolt="M8")]}, 400, "unknown bolt size"),
    ({"model_id": "MID", "cuts": [dict(LENGTHWISE, count=100)]}, 400, "too many bolts"),
    ({"model_id": "MID", "cuts": [dict(PINS, count=100)]}, 400, "too many pins"),
    ({"model_id": "MID", "cuts": [dict(PINS, params={"pin_diameter": 20})]}, 400, "between 8 and 12"),
    ({"model_id": "MID", "cuts": [dict(LENGTHWISE, plane={"origin": [0, 0, 900], "normal": [0, 0, 1]})]}, 400,
     "does not go through the model"),
])
def test_generate_errors_are_plain_json(client, model_id, payload, status, message):
    payload = {k: (model_id if v == "MID" else v) for k, v in payload.items()}
    r = client.post("/api/generate", json=payload)
    assert r.status_code == status and message in r.json["error"] and "detail" in r.json


def test_preview_reports_errors_per_cut(client, model_id):
    away = dict(LENGTHWISE, plane={"origin": [0, 0, 900], "normal": [0, 0, 1]})
    r = client.post("/api/preview", json={"model_id": model_id, "cuts": [LENGTHWISE, away]})
    assert r.status_code == 200
    assert r.json["cuts"][0]["error"] is None and "does not go through" in r.json["cuts"][1]["error"]
    assert r.json["cuts"][1]["derived"]["thickness"] == 4.0          # its own automatic values (decision 72)


def test_pin_flow(client, model_id):
    pv = strict_json(client.post("/api/preview", json={"model_id": model_id, "cuts": [PINS]}).data)["cuts"][0]
    d = pv["derived"]
    assert pv["error"] is None and pv["count"] == 8 and d["kind"] == "pin"
    assert (d["thickness"], d["bolt_line"], d["width"], d["hole_diameter"], d["washer"]) == (6.0, 7.0, 15.0, 8.3, 12.0)
    gen = client.post("/api/generate", json={"model_id": model_id, "cuts": [PINS]})
    assert gen.status_code == 200, gen.json
    out = strict_json(gen.data)
    assert out["hardware"] == []
    c = out["cuts"][0]
    assert (c["kind"], c["bolts"], c["nuts"], c["washers"], c["hole_diameter"]) == ("pin", 8, 0, 0, 8.3)
    assert {"diameter": 8.0, "length": 16.93, "head_side": "B", "push_from": ["B"], "needed": 8, "spares": 2,
            "count": 10}.items() <= c["pin"].items()
    assert c["joins"] == [{"parts": ["A", "B"], "bolts": 8}]
    assert [p["pins"] for p in out["parts"]] == [8, 8] and [p["bolts"] for p in out["parts"]] == [0, 0]
    (pins,) = out["pins"]
    assert pins["file"] == "demo_mold_pins_cut_1.stl" and pins["url"].endswith("/pins/1.stl")
    assert (pins["count"], pins["length"], pins["hole_diameter"], pins["same_as"]) == (10, 16.93, 8.3, None)
    r = client.get(pins["url"])
    assert r.status_code == 200 and pins["file"] in r.headers["Content-Disposition"]
    tm = reload_stl(r.data)
    assert tm.is_watertight and len(tm.split(only_watertight=False)) == 10 and abs(tm.bounds[0][2]) < 1e-6
    (fit,) = out["fit_tests"]
    assert fit["cuts"] == [1] and fit["hole_diameter"] == 8.3 and fit["file"] == "demo_mold_fit_test_cut_1.stl"
    tm = reload_stl(client.get(fit["url"]).data)
    assert tm.is_watertight and len(tm.split(only_watertight=False)) == 3
    z = zipfile.ZipFile(io.BytesIO(client.get(out["zip"]).data))
    assert set(z.namelist()) == {"demo_mold_part_A.stl", "demo_mold_part_B.stl", "demo_mold_pins_cut_1.stl",
                                 "demo_mold_fit_test_cut_1.stl", "report.txt", "settings.json"}
    settings = json.loads(z.read("settings.json"))
    saved = settings["cuts"][0]
    assert saved["bolt"] == "pin" and saved["params"]["pin_diameter"] == 8.0 and saved["params"]["pin_preload"] == 0.3
    again = client.post("/api/preview", json={"model_id": model_id, "cuts": settings["cuts"]}).json["cuts"][0]
    assert again["derived"]["kind"] == "pin" and again["derived"]["hole_diameter"] == 8.3
    report = z.read("report.txt").decode("utf-8")
    assert "demo_mold_pins_cut_1.stl" in report and "pushed in from part B" in report and "The fit test" in report
    r = client.get(f"/api/result/{out['result_id']}/fit/2.stl")
    assert r.status_code == 400 and "There is no fit test for cut 2" in r.json["error"]


def test_two_pin_cuts_api(client, model_id):
    out = client.post("/api/generate", json={"model_id": model_id,
                                             "cuts": [dict(PINS, params=LENGTHWISE["params"]), ACROSS]}).json
    z = zipfile.ZipFile(io.BytesIO(client.get(out["zip"]).data))
    assert {"demo_mold_pins_cut_1.stl", "demo_mold_pins_cut_2.stl", "demo_mold_fit_test_cut_1.stl"} <= set(z.namelist())
    assert not any("fit_test_cut_2" in n for n in z.namelist())
    assert out["fit_tests"][0]["cuts"] == [1, 2] and out["pins"][1]["same_as"] == 1
    assert client.get(f"/api/result/{out['result_id']}/fit/2.stl").status_code == 400


def test_bad_uploads(client):
    assert "not an STL" in upload(client, b"x", "photo.jpg").json["error"]
    junk = upload(client, b"hello, this is not a mesh", "notes.stl")
    assert junk.status_code == 400 and "STL file" in junk.json["error"]
    box = trimesh.creation.box((20, 20, 20))
    box.update_faces([i != 0 for i in range(len(box.faces))])
    r = upload(client, box.export(file_type="stl"), "open.stl")
    assert r.status_code == 400 and "has holes" in r.json["error"]
    small = upload(client, trimesh.creation.box((2, 3, 4)).export(file_type="stl"), "small.stl").json
    assert small["warnings"][0]["code"] == "units_small"


def test_unexpected_errors_are_plain_json(client, monkeypatch, model_id):
    from flangecutter.geometry import cutter
    monkeypatch.setattr(cutter, "preview", lambda *a: 1 / 0)
    r = client.post("/api/preview", json={"model_id": model_id, **LENGTHWISE})
    assert r.status_code == 500 and "Something went wrong" in r.json["error"]


def test_prefs_round_trip(client, home):
    assert client.get("/api/prefs").json == {}
    prefs = {"bolt": "M5", "side": "inside", "params": {"edge": 3}}
    assert client.post("/api/prefs", json=prefs).json == {"ok": True}
    assert client.get("/api/prefs").json == prefs
    assert (app_dir() / "settings.json").exists() and "Jiří Novák" in str(app_dir())


def test_requests_from_other_sites_are_refused(client):
    assert client.post("/api/shutdown", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/ping", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/shutdown", json={}, headers={"Origin": "http://127.0.0.1:5000"}).status_code == 200


def test_javascript_is_served_as_javascript():
    import mimetypes
    assert mimetypes.guess_type("main.js")[0] == "text/javascript"


def test_single_instance_lock():
    assert launcher.running_instance() is None
    app = create_app(Session())
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (app_dir() / launcher.LOCK_NAME).write_text(json.dumps({"port": server.server_port, "pid": 1}))
        assert launcher.running_instance() == (f"http://127.0.0.1:{server.server_port}/", __version__)
    finally:
        server.shutdown()
    assert launcher.running_instance() is None          # stale lock file: the next start takes over


def test_other_version_is_closed_on_request(monkeypatch):
    """Portable copies: when another version still runs, the user can close it and start this one."""
    app = create_app(Session())
    server = make_server("127.0.0.1", 0, app, threaded=True)
    app.extensions["shutdown"] = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    (app_dir() / launcher.LOCK_NAME).write_text(json.dumps({"port": server.server_port, "pid": 1}))
    try:
        monkeypatch.setattr(launcher, "_interactive", lambda: False)
        assert not launcher.replace_other_version(url, "1.0.0")          # nobody to ask: keep it running
        assert launcher.running_instance() == (url, __version__)
        answers = []
        monkeypatch.setattr(launcher, "_interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": answers.append(prompt) or "")
        assert launcher.replace_other_version(url, "1.0.0")              # Enter: it quits
        assert len(answers) == 1 and launcher.running_instance() is None
    finally:
        server.shutdown()
