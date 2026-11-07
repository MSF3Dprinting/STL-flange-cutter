"""Start Flange Cutter: `python -m flangecutter` (or FlangeCutter.exe).

Runs a local web server on 127.0.0.1 only (a free port) and opens the default browser.
If Flange Cutter is already running, it only opens the browser on the running app. If another
version is running (an older portable copy, for example), it offers to close that one first.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.request
import webbrowser

from . import APP_NAME, __version__
from .session import app_dir      # light: the geometry libraries load only after the single-instance check

LOCK_NAME = "flangecutter.lock"
log = logging.getLogger("flangecutter")
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1: never ask a proxy


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


def running_instance() -> tuple[str, str] | None:
    """(URL, version) of a Flange Cutter that is already running, from the lock file, or None."""
    try:
        info = json.loads((app_dir() / LOCK_NAME).read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/"
        with _LOCAL.open(url + "api/ping", timeout=2) as r:
            ping = json.load(r)
        return (url, str(ping.get("version", "?"))) if ping.get("app") == "flangecutter" else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def stop_instance(url: str, wait: float = 15.0) -> bool:
    """Ask a running Flange Cutter to quit, as its Quit button does. True once it no longer answers."""
    request = urllib.request.Request(url + "api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        _LOCAL.open(request, timeout=5).close()
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if running_instance() is None:
            return True
        time.sleep(0.2)
    return False


def _interactive() -> bool:
    """True when someone can answer in the console window (a double-click start)."""
    return bool(sys.stdin) and sys.stdin.isatty()


def replace_other_version(url: str, version: str) -> bool:
    """Another version is running, for example an older portable copy in another folder. It keeps the
    lock and the session, so this one cannot start next to it. Offer to close it.

    Returns True when the other version has quit and this one may start.
    """
    print(f"{APP_NAME} {version} is still running: {url}")
    if not _interactive():
        return False
    print(f"To start {APP_NAME} {__version__}, the running version must close. "
          "Save your settings in it first if you need them.")
    try:
        input("Press Enter to close it now, or close this window to keep it. ")
    except EOFError:
        return False
    if stop_instance(url):
        return True
    print("The running version did not close. Click Quit in it, then start Flange Cutter again.")
    try:
        input("Press Enter to close this window. ")
    except EOFError:
        pass
    return False


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

    found = running_instance()
    if found and found[1] != __version__ and replace_other_version(*found):
        found = None
    if found:
        url = found[0]
        print(f"{APP_NAME} is already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    from werkzeug.serving import make_server
    from .server import create_app            # numpy, trimesh, manifold3d: only now (a second start is quick)
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
