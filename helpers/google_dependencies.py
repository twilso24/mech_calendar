"""Provision Google Calendar dependencies for marketplace installations."""
from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from helpers.print_style import PrintStyle


_LOCK = threading.Lock()
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_REQUIREMENTS_FILE = _PLUGIN_DIR / "requirements.txt"
_REQUIRED_MODULES = {
    "google_auth_oauthlib": "google-auth-oauthlib",
    "googleapiclient": "google-api-python-client",
}
_INSTALL_TIMEOUT_SECONDS = 120


def _missing_modules() -> list[str]:
    missing = []
    for module in _REQUIRED_MODULES:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
                continue
            importlib.import_module(module)
        except Exception:
            missing.append(module)
    return missing


def missing_google_modules() -> list[str]:
    """Return required Google modules unavailable in the active interpreter."""
    return _missing_modules()


def ensure_google_dependencies() -> bool:
    """Install missing Google modules into the active framework runtime."""
    with _LOCK:
        missing = _missing_modules()
        if not missing:
            return True

        try:
            _install_google_dependencies()
            importlib.invalidate_caches()
        except Exception as exc:
            names = ", ".join(missing)
            raise RuntimeError(
                "Mech.Calendar could not install Google Calendar dependencies "
                f"({names}); installer error: {type(exc).__name__}"
            ) from exc

        remaining = _missing_modules()
        if remaining:
            raise RuntimeError(
                "Mech.Calendar Google Calendar dependencies remain unavailable "
                f"after installation: {', '.join(remaining)}"
            )
        return True


def _install_google_dependencies() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("'uv' is required to install Google Calendar dependencies")
    if not _REQUIREMENTS_FILE.is_file():
        raise RuntimeError(f"requirements file not found: {_REQUIREMENTS_FILE}")

    PrintStyle.info("Mech.Calendar: installing Google Calendar dependencies")
    subprocess.run(
        [uv, "pip", "install", "--python", sys.executable, "-r", str(_REQUIREMENTS_FILE)],
        cwd=str(_PLUGIN_DIR),
        check=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
