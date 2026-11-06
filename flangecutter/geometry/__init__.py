"""Geometry core: no web code here, so it can be tested and reused on its own.

- bolts.py   ISO data, fastening types, parameter metadata, derived defaults
- mesh_io.py load, check and repair STL files; write STL and ZIP
- cutter.py  cut planes, flange outlines, bolts, 3D build, checks
"""


class CutError(ValueError):
    """A problem the user can fix.

    `message` is plain language for the screen; `detail` is technical and goes to the log.
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


def warning(code: str, message: str, action: str) -> dict:
    """One warning for the user: what happened and what to do about it."""
    return {"code": code, "message": message, "action": action}
