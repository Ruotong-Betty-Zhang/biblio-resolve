"""
build_exe.py
------------
Build "Literature Lookup.exe": one file a client can download and double-click.

    python build_exe.py

Steps:
1. Creates a clean virtual environment (../.venv-build) holding only the
   packages in requirements-exe.txt. Building from your everyday Python would
   let PyInstaller pull in whatever else is installed there (PyTorch, ...).
2. Runs PyInstaller in one-file, windowed mode. The result is
   dist/Literature Lookup.exe.

Nothing personal is bundled: settings (API keys, email) and caches are not
part of the build, and the packaged app keeps its own in
%APPDATA%\\Literature Lookup (see app_paths.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(os.path.dirname(HERE), ".venv-build")
APP_NAME = "Literature Lookup"
# Optional features the exe deliberately leaves out (see requirements-exe.txt).
EXCLUDED_MODULES = ["torch", "sentence_transformers", "transformers", "fitz", "pytesseract",
                    "IPython", "jupyter", "notebook", "pytest"]


def venv_python():
    folder = "Scripts" if os.name == "nt" else "bin"
    return os.path.join(VENV_DIR, folder, "python.exe" if os.name == "nt" else "python")


def run(*command):
    print(">", " ".join(f'"{part}"' if " " in part else part for part in command), flush=True)
    subprocess.run(command, check=True, cwd=HERE)


def main():
    if not os.path.exists(venv_python()):
        print(f"Creating build environment in {VENV_DIR}")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    python = venv_python()
    run(python, "-m", "pip", "install", "--upgrade", "pip")
    run(python, "-m", "pip", "install", "-r", "requirements-exe.txt")

    data_separator = ";" if os.name == "nt" else ":"
    command = [
        python, "-m", "PyInstaller", "literature_lookup.py",
        "--name", APP_NAME,
        "--onefile",            # a single exe to hand out
        "--windowed",           # no console window behind the app
        "--noconfirm", "--clean",
        "--add-data", f"theme.json{data_separator}.",
        "--collect-data", "customtkinter",  # its built-in themes and fonts
        "--collect-data", "langdetect",     # language profiles used by Translate
    ]
    icon = os.path.join(HERE, "app.ico")
    if os.path.exists(icon):
        command += ["--icon", icon]
    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]
    run(*command)

    exe = os.path.join(HERE, "dist", APP_NAME + (".exe" if os.name == "nt" else ""))
    size = os.path.getsize(exe) / (1024 * 1024)
    print(f"\nBuilt {exe} ({size:.0f} MB)")


if __name__ == "__main__":
    sys.exit(main())
