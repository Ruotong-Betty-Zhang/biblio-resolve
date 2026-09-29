"""
app_paths.py
------------
Where the app keeps files it writes: settings (API keys, enabled sources)
and the lookup / verification / abstract / translation / full-text caches.

- Running from source: next to the code, as before, so a developer's
  existing settings and caches keep working.
- Packaged (PyInstaller exe): the code sits in a temporary folder that is
  deleted when the app closes (one-file build) or in a read-only install
  folder, so everything goes to the user's own data folder instead:
  %APPDATA%\\Literature Lookup on Windows, ~/Library/Application Support/
  Literature Lookup on macOS, ~/.local/share/Literature Lookup elsewhere.
"""
from __future__ import annotations

import os
import sys

APP_NAME = "Literature Lookup"


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def data_dir():
    if not is_frozen():
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    folder = os.path.join(base, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def data_file(name):
    """Path of a writable settings/cache file."""
    return os.path.join(data_dir(), name)
