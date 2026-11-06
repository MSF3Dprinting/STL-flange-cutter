"""Start Flange Cutter: `python -m flangecutter` (or FlangeCutter.exe).

Runs a local web server on 127.0.0.1 only (a free port) and opens the default browser.
If Flange Cutter is already running, it only opens the browser on the running app.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import urllib.request
import webbrowser

from werkzeug.serving import make_server

from . import APP_NAME, __version__
from .server import create_app
from .session import app_dir

LOCK_NAME = "flangecutter.lock"
log = logging.getLogger("flangecutter")


def setup_logging() -> str:
    """Rotating log file in the user's app folder; details of every error go there."""
    folder = app_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "flangecutter.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # no line per request in the console
    return str(path)


def running_instance() -> str | None:
    """URL of a Flange Cutter that is already running, from the lock file, or None."""
    try:
        info = json.loads((app_dir() / LOCK_NAME).read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never ask a proxy
        with opener.open(url + "api/ping", timeout=2) as r:
            return url if json.load(r).get("app") == "flangecutter" else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flangecutter", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # e.g. "C:\Users\Jiří Novák" on a console with an old code page
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    log_file = setup_logging()

    url = running_instance()
    if url:
        print(f"{APP_NAME} is already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    app = create_app()
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}/"
    lock = app_dir() / LOCK_NAME
    lock.write_text(json.dumps({"port": server.server_port, "pid": os.getpid(), "version": __version__}),
                    encoding="utf-8")
    app.extensions["shutdown"] = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
    log.info("%s %s started at %s", APP_NAME, __version__, url)
    print(f"{APP_NAME} {__version__} is running at {url}")
    print("Your browser opens the app. Close this window to quit.")
    print(f"Log file: {log_file}")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                lock.unlink()
        except (OSError, ValueError):
            pass
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
