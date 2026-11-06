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
from flangecutter.server import create_app
from flangecutter.session import Session, app_dir

LENGTHWISE = {"plane": {"origin": [0, 0, 120], "normal": [0, 1, 0], "x_axis": [1, 0, 0]},
              "side": "outside", "bolt": "M4", "count": 8, "fastening": "through_nut", "nut_pocket_half": "B",
              "params": {"thickness": None, "width": None, "wall_gap": 1.0, "edge": 2.0, "hole_clearance": 0.2,
                         "overlap": 0.6, "max_wall_angle": 25, "tool_clearance": 12}}


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
        assert launcher.running_instance() == f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
    assert launcher.running_instance() is None          # stale lock file: the next start takes over
