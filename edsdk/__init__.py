from __future__ import annotations

import os
from pathlib import Path

_DLL_SEARCH_ENV_VARS = (
    "EDSDK_PYTHON_DLL_DIR",
    "CANON_EDSDK_DLL_DIR",
)
_DLL_DIRECTORY_HANDLES: list[object] = []


def _candidate_dll_directories() -> list[Path]:
    """Return likely Canon SDK DLL directories for source and wheel installs."""
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parent

    candidates: list[Path] = []
    for env_name in _DLL_SEARCH_ENV_VARS:
        configured = os.environ.get(env_name, "").strip()
        if configured:
            candidates.append(Path(configured).expanduser())

    # Local source checkout / editable install.
    candidates.append(repo_root / "dependencies" / "EDSDK_64" / "Dll")
    # Compatibility with legacy bundled-wheel installs.
    candidates.append(package_dir)
    return candidates


def _register_windows_dll_directories() -> None:
    """Register Canon SDK DLL directories before importing the extension module."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return

    seen: set[str] = set()
    for candidate in _candidate_dll_directories():
        resolved = candidate.resolve(strict=False)
        key = str(resolved)
        if key in seen or not resolved.is_dir():
            continue
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(resolved)))
        seen.add(key)


_register_windows_dll_directories()

try:
    from edsdk.api import *
except ImportError as exc:
    if os.name == "nt":
        raise ImportError(
            "Failed to import edsdk.api. Install Canon EDSDK separately and set "
            "EDSDK_PYTHON_DLL_DIR or CANON_EDSDK_DLL_DIR to the folder containing "
            "EDSDK.dll and EdsImage.dll."
        ) from exc
    raise

from edsdk.constants import *
