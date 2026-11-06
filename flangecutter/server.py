"""Flask app: the web page and a small JSON API. Kept thin: the work happens in geometry/."""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from . import APP_NAME, __version__
from .geometry import CutError, bolts, cutter, mesh_io
from .session import Session, load_prefs, save_prefs

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
log = logging.getLogger("flangecutter")

# Some Windows registries map .js to text/plain; browsers then refuse to run ES modules.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def _stem(name: str) -> str:
    """A safe file name stem from the uploaded file name (keeps letters of any language)."""
    stem = re.sub(r"[^\w\-]+", "_", Path(name).stem, flags=re.UNICODE).strip("_")
    return stem[:60] or "model"


def settings_json(model: mesh_io.Model, cuts: list[cutter.Cut]) -> dict:
    """The settings file: the same cut objects the page sends, plus the model it belongs to."""
    return {"format": 1, "app": APP_NAME, "version": __version__,
            "model": {"name": model.name, "sha256": model.sha256}, "cuts": [c.settings for c in cuts]}


def create_app(session: Session | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=512 * 1024 * 1024, SEND_FILE_MAX_AGE_DEFAULT=0)
    app.json.sort_keys = False
    s = session or Session()
    app.extensions["session"] = s
    app.extensions["shutdown"] = None       # set by __main__ when a real server runs

    @app.before_request
    def local_only():
        """Refuse requests that a web site could forge (DNS rebinding, cross-site POST)."""
        if (request.host.rsplit(":", 1)[0].strip("[]") not in LOCAL_HOSTS
                or (request.origin and urlsplit(request.origin).hostname not in LOCAL_HOSTS)):
            return jsonify(error="Not allowed.", detail="request from outside this computer"), 403
        return None

    @app.errorhandler(CutError)
    def cut_error(e: CutError):
        log.info("user error: %s | %s", e.message, e.detail)
        return jsonify(error=e.message, detail=e.detail), 400

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        message = "The file is too large." if e.code == 413 else (e.description or e.name)
        return jsonify(error=message, detail=f"{e.code} {e.name}"), e.code

    @app.errorhandler(Exception)
    def crash(e: Exception):
        log.exception("unexpected error")
        return jsonify(error="Something went wrong. Try again. If it keeps happening, send the log file "
                             "to your support person.", detail=repr(e)), 500

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/api/ping")
    def ping():
        return jsonify(app="flangecutter", version=__version__)

    @app.get("/api/bolts")
    def bolt_catalog():
        return jsonify(bolts.catalog() | {"version": __version__})

    @app.post("/api/model")
    def open_model():
        f = request.files.get("file")
        if f is None or not f.filename:
            raise CutError("Choose an STL file to open.")
        if not f.filename.lower().endswith(".stl"):
            raise CutError("This is not an STL file. Choose a file that ends in .stl.", f.filename)
        with s.work:
            s.set_progress("Opening the model", 0.2)
            try:
                model = mesh_io.load_stl(f.read(), f.filename)
            finally:
                s.set_progress("", 0.0, busy=False)
        mid = s.add_model(model)
        log.info("opened %s (%d triangles)", f.filename, len(model.mesh.faces))
        return jsonify(model.info() | {"model_id": mid})

    @app.get("/api/model/<mid>/mesh.stl")
    def model_mesh(mid: str):
        model = s.model(mid)
        data = model.mesh.export(file_type="stl")
        return send_file(io.BytesIO(data), mimetype="model/stl", download_name="model.stl")

    @app.post("/api/preview")
    def preview():
        body = request.get_json(silent=True) or {}
        model, cuts = s.model(body.get("model_id")), cutter.parse_cuts(body)
        with s.work:
            return jsonify(cutter.preview(model, cuts))

    @app.post("/api/generate")
    def generate():
        body = request.get_json(silent=True) or {}
        model, cuts = s.model(body.get("model_id")), cutter.parse_cuts(body)
        if not s.work.acquire(timeout=120):
            raise CutError("The app is still busy with the last job. Wait a moment and try again.")
        try:
            result = cutter.generate(model, cuts, progress=s.set_progress)
        finally:
            s.set_progress("", 0.0, busy=False)
            s.work.release()
        stem = _stem(model.name)
        files = {p.name: f"{stem}_part_{p.name}.stl" for p in result.parts}
        rid = s.add_result({"result": result, "model": model, "files": files, "settings": settings_json(model, cuts)})
        out = cutter.summary(result)
        for part in out["parts"]:
            part["file"] = files[part["name"]]
            part["url"] = f"/api/result/{rid}/{part['name']}.stl"
        log.info("generated %d parts in %.2f s", len(result.parts), result.seconds)
        return jsonify(out | {"result_id": rid, "zip": f"/api/result/{rid}/bundle.zip"})

    @app.get("/api/progress")
    def progress():
        return jsonify(s.progress)

    @app.get("/api/result/<rid>/<name>.stl")
    def part_stl(rid: str, name: str):
        entry = s.result(rid)
        part = next((p for p in entry["result"].parts if p.name == name), None)
        if part is None:
            raise CutError(f"There is no part {name} in these results.")
        return send_file(io.BytesIO(part.stl()), mimetype="model/stl", as_attachment=True,
                         download_name=entry["files"][name])

    @app.get("/api/result/<rid>/bundle.zip")
    def bundle(rid: str):
        entry = s.result(rid)
        result, model, files = entry["result"], entry["model"], entry["files"]
        content = {files[p.name]: p.stl() for p in result.parts}
        content["report.txt"] = cutter.report_text(result, model.name, model.sha256, __version__, files)
        content["settings.json"] = json.dumps(entry["settings"], indent=1, ensure_ascii=False)
        return send_file(io.BytesIO(mesh_io.zip_bytes(content)), mimetype="application/zip", as_attachment=True,
                         download_name=f"{_stem(model.name)}_parts.zip")

    @app.get("/api/prefs")
    def get_prefs():
        return jsonify(load_prefs())

    @app.post("/api/prefs")
    def put_prefs():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise CutError("These settings could not be saved.", "body is not a JSON object")
        save_prefs(body)
        return jsonify(ok=True)

    @app.post("/api/shutdown")
    def shutdown():
        stop = app.extensions.get("shutdown")
        if stop:
            stop()
        return jsonify(ok=True)

    return app
