"""Entry point of FlangeCutter.exe (PyInstaller): does the same as `python -m flangecutter`.

flangecutter/__main__.py uses relative imports, so PyInstaller needs this small script as its start file.
"""
import sys

from flangecutter.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
