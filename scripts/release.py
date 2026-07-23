"""Release automation for edsdk-python.

Builds win_amd64 wheels for multiple Python versions, verifies each wheel by
installing it into a throwaway venv and running the unit tests, then creates
a GitHub Release with the wheels attached.

Canon SDK files under dependencies/ are required locally to build, but they
are never uploaded anywhere: only wheels containing this project's own code
are published.

Usage:
    python scripts/release.py --dry-run   # build + verify only
    python scripts/release.py             # build + verify + GitHub Release
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

DEFAULT_REPO = "maedayoshiaki/edsdk-python"
DEFAULT_PYTHONS = ("3.11", "3.12", "3.13")
SDK_FILES = (
    "dependencies/EDSDK/Header/EDSDK.h",
    "dependencies/EDSDK_64/Library/EDSDK.lib",
    "dependencies/EDSDK_64/Dll/EDSDK.dll",
    "dependencies/EDSDK_64/Dll/EdsImage.dll",
)


def parse_project_version(pyproject_text: str) -> str:
    """Return project.version from pyproject.toml text."""
    data = tomllib.loads(pyproject_text)
    try:
        return data["project"]["version"]
    except KeyError as exc:
        raise ValueError("project.version not found in pyproject.toml") from exc


def wheel_filename(version: str, python: str) -> str:
    """Return the wheel file name produced for a Python version like '3.11'."""
    major, minor = python.split(".")
    tag = f"cp{major}{minor}"
    return f"edsdk_python-{version}-{tag}-{tag}-win_amd64.whl"


def wheel_url(repo: str, version: str, python: str) -> str:
    """Return the GitHub Release download URL for one wheel."""
    return (
        f"https://github.com/{repo}/releases/download/"
        f"v{version}/{wheel_filename(version, python)}"
    )


def _sorted_desc(pythons: Sequence[str]) -> list[str]:
    return sorted(pythons, key=lambda p: tuple(map(int, p.split("."))), reverse=True)


def consumer_snippet(repo: str, version: str, pythons: Sequence[str]) -> str:
    """Return a pyproject.toml dependency block with per-version URL markers."""
    lines = ["dependencies = ["]
    for python in _sorted_desc(pythons):
        lines.append(
            f'  "edsdk-python @ {wheel_url(repo, version, python)}'
            f" ; python_version == '{python}'\","
        )
    lines.append("]")
    return "\n".join(lines)


def release_notes(repo: str, version: str, pythons: Sequence[str]) -> str:
    """Return the GitHub Release notes body (markdown)."""
    newest = _sorted_desc(pythons)[0]
    versions_label = " / ".join(sorted(pythons))
    return f"""## edsdk-python v{version}

Windows (win_amd64) wheels for Python {versions_label}.

The wheels contain only this project's code. Canon EDSDK (EDSDK.dll /
EdsImage.dll) is NOT included: obtain it through Canon's developer programme
and set `EDSDK_PYTHON_DLL_DIR` (or `CANON_EDSDK_DLL_DIR`) to the DLL folder
before importing.

### Install with pip

```
pip install {wheel_url(repo, version, newest)}
```

### pyproject.toml (pip / uv)

```toml
{consumer_snippet(repo, version, pythons)}
```
"""


def check_sdk_files(repo_root: Path) -> list[str]:
    """Return Canon SDK files (relative paths) missing under repo_root."""
    return [rel for rel in SDK_FILES if not (repo_root / rel).is_file()]
