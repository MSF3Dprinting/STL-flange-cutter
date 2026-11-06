"""State of the one local user: the open model and recent results (in memory), plus the small
files the app keeps in its folder: preferences, lock file and logs. Nothing else is stored."""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

from .geometry import CutError
from .geometry.mesh_io import Model

KEEP_RESULTS = 3
MAX_PREFS_BYTES = 64 * 1024


def app_dir() -> Path:
    """%LOCALAPPDATA%\\FlangeCutter on Windows, ~/.flangecutter elsewhere; FLANGECUTTER_HOME overrides."""
    if os.environ.get("FLANGECUTTER_HOME"):
        base = Path(os.environ["FLANGECUTTER_HOME"])
    elif os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "FlangeCutter"
    else:
        base = Path.home() / ".flangecutter"
    base.mkdir(parents=True, exist_ok=True)
    return base


class Session:
    """One model at a time (opening a new one forgets the old one) and the last few results."""

    def __init__(self) -> None:
        self.lock = threading.Lock()          # guards the dictionaries below
        self.work = threading.Lock()          # one geometry job at a time
        self.models: dict[str, Model] = {}
        self.results: dict[str, dict] = {}
        self.progress = {"busy": False, "stage": "", "fraction": 0.0}

    def add_model(self, model: Model) -> str:
        mid = secrets.token_hex(6)
        with self.lock:
            self.models = {mid: model}
            self.results.clear()
        return mid

    def model(self, mid: str) -> Model:
        with self.lock:
            m = self.models.get(str(mid))
        if m is None:
            raise CutError("This model is no longer open. Open the STL file again.", f"model_id {mid!r}")
        return m

    def add_result(self, entry: dict) -> str:
        rid = secrets.token_hex(6)
        with self.lock:
            self.results[rid] = entry
            while len(self.results) > KEEP_RESULTS:
                self.results.pop(next(iter(self.results)))
        return rid

    def result(self, rid: str) -> dict:
        with self.lock:
            r = self.results.get(str(rid))
        if r is None:
            raise CutError("These parts are no longer available. Click Generate again.", f"result_id {rid!r}")
        return r

    def set_progress(self, stage: str, fraction: float, busy: bool = True) -> None:
        self.progress = {"busy": busy, "stage": stage, "fraction": round(float(fraction), 3)}


def load_prefs() -> dict:
    """The user's last settings (bolt size, flange values...), or {} if there are none."""
    try:
        data = json.loads((app_dir() / "settings.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    text = json.dumps(prefs, indent=1, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_PREFS_BYTES:
        raise CutError("These settings are too large to save.")
    path = app_dir() / "settings.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)            # atomic: an interrupted save never leaves a broken file
