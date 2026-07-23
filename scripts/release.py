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


def run(
    cmd: Sequence[object],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, echoing it first. Raises on non-zero exit."""
    printable = " ".join(str(part) for part in cmd)
    print(f"+ {printable}", flush=True)
    return subprocess.run(
        [str(part) for part in cmd],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_output(repo_root: Path, *args: str) -> str:
    return run(["git", "-C", repo_root, *args], capture=True).stdout.strip()


def remote_main_version(repo_root: Path) -> str:
    """Return project.version as committed on origin/main."""
    run(["git", "-C", repo_root, "fetch", "origin", "main"], capture=True)
    text = git_output(repo_root, "show", "origin/main:pyproject.toml")
    return parse_project_version(text)


def build_wheels(repo_root: Path, version: str, pythons: Sequence[str]) -> list[Path]:
    """Build one wheel per Python version into dist/ and return their paths."""
    dist = repo_root / "dist"
    dist.mkdir(exist_ok=True)
    wheels: list[Path] = []
    for python in pythons:
        expected = dist / wheel_filename(version, python)
        expected.unlink(missing_ok=True)
        run(["uv", "build", "--wheel", "--python", python], cwd=repo_root)
        if not expected.is_file():
            raise SystemExit(f"ERROR: expected wheel was not produced: {expected}")
        wheels.append(expected)
    return wheels


def smoke_test(repo_root: Path, wheel: Path, python: str) -> None:
    """Install the wheel into a throwaway venv and run the unit tests there.

    Runs in a temporary directory so the repository's edsdk/ source tree
    cannot shadow the installed package.
    """
    dll_dir = repo_root / "dependencies" / "EDSDK_64" / "Dll"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        work = Path(td)
        venv_dir = work / "venv"
        run(["uv", "venv", "--python", python, venv_dir])
        venv_python = venv_dir / "Scripts" / "python.exe"
        run(["uv", "pip", "install", "--python", venv_python, wheel, "pytest"])
        shutil.copy2(
            repo_root / "tests" / "test_exposure.py", work / "test_exposure.py"
        )
        env = os.environ.copy()
        env["EDSDK_PYTHON_DLL_DIR"] = str(dll_dir)
        run(
            [venv_python, "-m", "pytest", "test_exposure.py", "-q"],
            cwd=work,
            env=env,
        )


def create_release(
    repo_root: Path,
    repo: str,
    version: str,
    pythons: Sequence[str],
    wheels: Sequence[Path],
) -> None:
    notes = release_notes(repo, version, pythons)
    run(
        [
            "gh",
            "release",
            "create",
            f"v{version}",
            *wheels,
            "--repo",
            repo,
            "--title",
            f"v{version}",
            "--notes",
            notes,
        ],
        cwd=repo_root,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and verify wheels without creating a release",
    )
    parser.add_argument(
        "--skip-smoke", action="store_true", help="skip wheel install verification"
    )
    parser.add_argument(
        "--pythons", nargs="+", default=list(DEFAULT_PYTHONS), metavar="X.Y"
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, metavar="OWNER/NAME")
    parser.add_argument(
        "--allow-branch",
        action="store_true",
        help="allow releasing from a branch other than main",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent

    problems = [f"missing Canon SDK file: {rel}" for rel in check_sdk_files(repo_root)]
    required_tools = ("uv",) if args.dry_run else ("uv", "gh")
    for tool in required_tools:
        if shutil.which(tool) is None:
            problems.append(f"required tool not found on PATH: {tool}")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1

    version = parse_project_version(
        (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    mode = "dry run" if args.dry_run else "release"
    print(f"edsdk-python v{version} ({mode}) for Python {', '.join(args.pythons)}")

    if git_output(repo_root, "status", "--porcelain"):
        print("ERROR: working tree is not clean", file=sys.stderr)
        return 1

    if not args.dry_run:
        branch = git_output(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main" and not args.allow_branch:
            print(
                f"ERROR: releases are created from main (current branch: {branch}); "
                "pass --allow-branch to override",
                file=sys.stderr,
            )
            return 1
        if git_output(repo_root, "tag", "-l", f"v{version}"):
            print(f"ERROR: tag v{version} already exists locally", file=sys.stderr)
            return 1
        if git_output(repo_root, "ls-remote", "--tags", "origin", f"refs/tags/v{version}"):
            print(f"ERROR: tag v{version} already exists on origin", file=sys.stderr)
            return 1
        remote_version = remote_main_version(repo_root)
        if remote_version != version:
            print(
                f"ERROR: origin/main has version {remote_version} but local pyproject "
                f"has {version}; merge to main before releasing",
                file=sys.stderr,
            )
            return 1
        run(["gh", "auth", "status"], capture=True)

    wheels = build_wheels(repo_root, version, args.pythons)

    if args.skip_smoke:
        print("Skipping smoke tests (--skip-smoke)")
    else:
        for python, wheel in zip(args.pythons, wheels):
            print(f"--- smoke test: {wheel.name} (Python {python}) ---")
            smoke_test(repo_root, wheel, python)

    if args.dry_run:
        print("Dry run complete. Wheels in dist/:")
        for wheel in wheels:
            print(f"  {wheel.name}")
    else:
        create_release(repo_root, args.repo, version, args.pythons, wheels)
        print(
            f"Release v{version} created: "
            f"https://github.com/{args.repo}/releases/tag/v{version}"
        )

    print("\nConsumer pyproject.toml snippet:\n")
    print(consumer_snippet(args.repo, version, args.pythons))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(str(part) for part in exc.cmd)
        print(f"ERROR: command failed with exit code {exc.returncode}: {cmd}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        sys.exit(1)
