# PyInstaller spec for FlangeCutter.exe: a one-folder build with a console window.
#
# build\build_windows.ps1 runs it. By hand, from the project folder:
#   python -m PyInstaller --noconfirm --clean --distpath build\out\dist --workpath build\out\work build\flangecutter.spec
# Result: build\out\dist\FlangeCutter\FlangeCutter.exe, with Python and all libraries in _internal\.
#
# One folder instead of one file, and no UPX: the app starts faster and antivirus programs raise
# fewer false alarms (spec section 6).
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parent          # SPECPATH = the folder of this file (build/)
NAME = "FlangeCutter"

# The version comes from flangecutter/__init__.py, like everywhere else.
VERSION = re.search(r'^__version__\s*=\s*"([^"]+)"',
                    (ROOT / "flangecutter" / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)
NUMBERS = tuple(int(n) for n in re.match(r"\d+(?:\.\d+){0,3}", VERSION).group().split("."))
NUMBERS = (NUMBERS + (0, 0, 0))[:4]

# The web page (incl. vendor/three) goes to the same place relative to the code, so server.py
# finds it with Path(__file__).parent / "static".
datas = [(str(ROOT / "flangecutter" / "static"), "flangecutter/static")]
# Package metadata: importlib.metadata needs it, and it carries each package's license files
# (GEOS in shapely is LGPL, so its license must ship with the DLL).
for dist in ("flask", "werkzeug", "jinja2", "markupsafe", "itsdangerous", "click", "blinker", "colorama",
             "numpy", "shapely", "trimesh", "manifold3d", "networkx"):
    datas += copy_metadata(dist)
for source, folder in ((Path(sys.base_prefix) / "LICENSE.txt", "licenses/python"),
                       (ROOT / "LICENSE", "licenses/flangecutter")):
    if source.exists():
        datas += [(str(source), folder)]

# Never needed at run time. Most are optional imports of trimesh or numpy; listing them keeps the
# bundle the same whatever else is installed in the build environment.
EXCLUDES = ["tkinter", "_tkinter", "scipy", "matplotlib", "PIL", "pandas", "sympy", "lxml", "pyglet",
            "rtree", "embreex", "IPython", "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "_pytest",
            "playwright", "setuptools", "pkg_resources", "_distutils_hack", "pip"]

a = Analysis([str(ROOT / "build" / "launch.py")], pathex=[str(ROOT)], datas=datas, excludes=EXCLUDES)
pyz = PYZ(a.pure)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[StringFileInfo([StringTable("040904B0", [
              StringStruct("CompanyName", "SilvestrTk"),
              StringStruct("FileDescription", "Flange Cutter"),
              StringStruct("FileVersion", VERSION),
              StringStruct("InternalName", NAME),
              StringStruct("LegalCopyright", "Copyright (c) SilvestrTk. MIT License."),
              StringStruct("OriginalFilename", f"{NAME}.exe"),
              StringStruct("ProductName", "Flange Cutter"),
              StringStruct("ProductVersion", VERSION)])]),
          VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME,
          console=True,                   # the small window that says "Close this window to quit"
          upx=False, icon=str(ROOT / "build" / "flangecutter.ico"), version=version_info)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name=NAME)
