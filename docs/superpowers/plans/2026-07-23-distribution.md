# GitHub Releases 配布改善 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub Releases に cp311/cp312/cp313 の wheel を添付し、消費側リポジトリが pyproject.toml の URL 指定だけで導入できるようにする(リリース自動化スクリプト + ドキュメント)。

**Architecture:** `scripts/release.py`(標準ライブラリのみ)が前提チェック → `uv build` で 3 バージョンの wheel ビルド → 各 wheel を一時 venv にインストールしてユニットテスト実行 → `gh release create` でリリース作成、を一括実行する。純粋関数(バージョン解析・ファイル名・スニペット生成・SDK ファイルチェック)はユニットテスト対象。README に消費側/保守者向けセクションを追加。

**Tech Stack:** Python 3.13(stdlib のみ: tomllib / argparse / subprocess / tempfile / shutil / pathlib)、uv、gh CLI、pytest

**設計ドキュメント:** `docs/superpowers/specs/2026-07-23-distribution-design.md`

## Global Constraints

- Canon 由来ファイル(`dependencies/` 配下の EDSDK.h / EDSDK.lib / EDSDK.dll / EdsImage.dll 等)を **コミット・アップロード・コピーしてはならない**。スクリプトがアップロードするのは `dist/edsdk_python-*.whl` のみ
- Python 実行は必ず `.venv\Scripts\python.exe`(3.13)。システム python は使用不可
- `scripts/release.py` は **標準ライブラリのみ**(外部依存を追加しない)
- ライブラリ本体の API・挙動変更禁止。`edsdk/__init__.py` は ImportError メッセージ文字列の追記のみ
- リポジトリスラッグは `maedayoshiaki/edsdk-python`、対応 Python は `3.11 / 3.12 / 3.13`、バージョンは `0.1.7`
- wheel ファイル名形式: `edsdk_python-{version}-cp3XX-cp3XX-win_amd64.whl`(例: `edsdk_python-0.1.7-cp311-cp311-win_amd64.whl`)
- コミットメッセージは既存規約(`feat:` / `test:` / `docs:` / `upgrade:` + 日本語)に合わせる

---

### Task 1: バージョン bump + release.py 純粋関数とユニットテスト

**Files:**
- Modify: `pyproject.toml:12`
- Create: `scripts/release.py`
- Test: `tests/test_release_script.py`

**Interfaces:**
- Consumes: なし(独立)
- Produces(Task 2 が使用):
  - `parse_project_version(pyproject_text: str) -> str`
  - `wheel_filename(version: str, python: str) -> str`
  - `wheel_url(repo: str, version: str, python: str) -> str`
  - `consumer_snippet(repo: str, version: str, pythons: Sequence[str]) -> str`
  - `release_notes(repo: str, version: str, pythons: Sequence[str]) -> str`
  - `check_sdk_files(repo_root: Path) -> list[str]`
  - 定数 `DEFAULT_REPO = "maedayoshiaki/edsdk-python"`, `DEFAULT_PYTHONS = ("3.11", "3.12", "3.13")`, `SDK_FILES`

- [ ] **Step 1: バージョンを 0.1.7 に bump**

`pyproject.toml` の 12 行目を変更:

```toml
version = "0.1.7"  # GitHub Releases 配布ツーリング追加バージョン
```

- [ ] **Step 2: 失敗するテストを書く**

`tests/test_release_script.py` を作成:

```python
"""Unit tests for scripts/release.py pure helpers (no camera/SDK required)."""

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import release  # noqa: E402


class TestParseProjectVersion:
    def test_reads_version(self):
        text = '[project]\nname = "edsdk-python"\nversion = "0.1.7"\n'
        assert release.parse_project_version(text) == "0.1.7"

    def test_missing_version_raises(self):
        with pytest.raises(ValueError):
            release.parse_project_version('[project]\nname = "edsdk-python"\n')


class TestWheelNames:
    def test_wheel_filename_cp311(self):
        assert (
            release.wheel_filename("0.1.7", "3.11")
            == "edsdk_python-0.1.7-cp311-cp311-win_amd64.whl"
        )

    def test_wheel_filename_cp313(self):
        assert (
            release.wheel_filename("0.1.7", "3.13")
            == "edsdk_python-0.1.7-cp313-cp313-win_amd64.whl"
        )

    def test_wheel_url(self):
        assert release.wheel_url("owner/repo", "0.1.7", "3.12") == (
            "https://github.com/owner/repo/releases/download/"
            "v0.1.7/edsdk_python-0.1.7-cp312-cp312-win_amd64.whl"
        )


class TestConsumerSnippet:
    def test_is_valid_toml_with_markers_descending(self):
        snippet = release.consumer_snippet(
            "maedayoshiaki/edsdk-python", "0.1.7", ["3.11", "3.12", "3.13"]
        )
        parsed = tomllib.loads(snippet)
        deps = parsed["dependencies"]
        assert len(deps) == 3
        # 新しい Python が先頭(読みやすさのため降順)
        assert "python_version == '3.13'" in deps[0]
        assert "python_version == '3.12'" in deps[1]
        assert "python_version == '3.11'" in deps[2]
        assert all(dep.startswith("edsdk-python @ https://github.com/") for dep in deps)
        assert "cp313-cp313-win_amd64.whl" in deps[0]

    def test_urls_match_wheel_url(self):
        snippet = release.consumer_snippet("owner/repo", "0.2.0", ["3.13"])
        assert release.wheel_url("owner/repo", "0.2.0", "3.13") in snippet


class TestReleaseNotes:
    def test_contains_urls_and_dll_guidance(self):
        notes = release.release_notes(
            "maedayoshiaki/edsdk-python", "0.1.7", ["3.11", "3.12", "3.13"]
        )
        for python in ("3.11", "3.12", "3.13"):
            assert release.wheel_url("maedayoshiaki/edsdk-python", "0.1.7", python) in notes
        assert "EDSDK_PYTHON_DLL_DIR" in notes
        # Canon の SDK を同梱しない旨の明示
        assert "NOT included" in notes


class TestCheckSdkFiles:
    def test_all_missing(self, tmp_path):
        missing = release.check_sdk_files(tmp_path)
        assert len(missing) == 4
        assert "dependencies/EDSDK/Header/EDSDK.h" in missing

    def test_all_present(self, tmp_path):
        for rel in release.SDK_FILES:
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
        assert release.check_sdk_files(tmp_path) == []
```

- [ ] **Step 3: テストが失敗することを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_release_script.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'release'`)

- [ ] **Step 4: 純粋関数を実装**

`scripts/release.py` を作成:

```python
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
```

- [ ] **Step 5: テストが通ることを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_release_script.py -v`
Expected: PASS(9 件)

- [ ] **Step 6: 既存テストへの影響がないことを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exposure.py -q`
Expected: PASS(37 件)

- [ ] **Step 7: コミット**

```powershell
git add pyproject.toml scripts/release.py tests/test_release_script.py
git commit -m "feat: リリーススクリプトの基盤(純粋関数)追加とバージョン0.1.7へのbump"
```

---

### Task 2: release.py オーケストレーション + dry-run 統合検証

**Files:**
- Modify: `scripts/release.py`(Task 1 の関数群の後に追記)

**Interfaces:**
- Consumes: Task 1 の全関数・定数(シグネチャは Task 1 の Produces を参照)
- Produces: CLI エントリポイント `python scripts/release.py [--dry-run] [--skip-smoke] [--pythons ...] [--repo ...] [--allow-branch]`

**このタスクのテストはユニットテストではなく `--dry-run` の実行**(ビルド 3 回 + venv スモーク 3 回で数分かかる。MSVC・uv・Canon SDK が揃った本マシンで実行可能)。

- [ ] **Step 1: オーケストレーション部を実装**

`scripts/release.py` の `check_sdk_files` の後に追記:

```python
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
```

- [ ] **Step 2: ユニットテストが引き続き通ることを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_release_script.py -q`
Expected: PASS(9 件。オーケストレーション追記により import が壊れていないこと)

- [ ] **Step 3: dry-run 統合検証(コミット前に必ず作業ツリーをクリーンにする)**

`--dry-run` はクリーンな作業ツリーを要求するため、先に Step 4 のコミットを行ってから実行してもよい(推奨: 先にコミット → dry-run → 問題があれば修正して追加コミット)。

Run: `.venv\Scripts\python.exe scripts\release.py --dry-run`
Expected:
- 前提チェック通過(Canon SDK ファイル 4 点が存在)
- `uv build --wheel --python 3.11` / `3.12` / `3.13` が成功し、`dist/edsdk_python-0.1.7-cp311-cp311-win_amd64.whl` ほか計 3 wheel が生成される(3.11 は uv が自動ダウンロード)
- 各 wheel のスモークテスト: 一時 venv で `test_exposure.py` 37 件 PASS × 3
- `Dry run complete.` と消費側スニペットが出力される
- GitHub Release・タグは作成されない

うまくいかない場合の注意:
- `uv build --wheel --python 3.11` が Python を見つけられない場合は `uv python install 3.11` を先に実行
- ビルドは MSVC(Visual Studio Build Tools)を要求する。本マシンでは過去に cp310/cp312/cp313 のビルド実績があるため通常は成功する

- [ ] **Step 4: コミット**

```powershell
git add scripts/release.py
git commit -m "feat: リリーススクリプトにビルド・検証・リリース作成の一括実行を実装"
```

(dry-run を Step 4 の後に実行した場合、修正が発生したら追加コミットする)

---

### Task 3: README 消費側/保守者ガイドと ImportError メッセージ改善

**Files:**
- Modify: `README.md`(冒頭セクションの直後に消費側ガイド、`## Exposure control` の前に保守者ガイド)
- Modify: `edsdk/__init__.py:51-56`

**Interfaces:**
- Consumes: Task 1 の URL 形式(`https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.7/edsdk_python-0.1.7-cp3XX-cp3XX-win_amd64.whl`)。コードは使用しない
- Produces: なし(ドキュメントのみ)

- [ ] **Step 1: README に消費側ガイドを追加**

`README.md` の冒頭説明(`EDSDK_PYTHON_DLL_DIR` / `CANON_EDSDK_DLL_DIR` に触れている段落、11 行目付近)の直後、`## Obtain the EDSDK from Canon` の前に以下のセクションを挿入:

````markdown
## Install from GitHub Releases (consumer projects)

Prebuilt Windows wheels (64-bit, Python 3.11 / 3.12 / 3.13) are attached to
[GitHub Releases](https://github.com/maedayoshiaki/edsdk-python/releases).
The wheels contain only this project's code — Canon's EDSDK is **not** included.

Add to your `pyproject.toml` (works with both pip and uv):

```toml
dependencies = [
  "edsdk-python @ https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.7/edsdk_python-0.1.7-cp313-cp313-win_amd64.whl ; python_version == '3.13'",
  "edsdk-python @ https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.7/edsdk_python-0.1.7-cp312-cp312-win_amd64.whl ; python_version == '3.12'",
  "edsdk-python @ https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.7/edsdk_python-0.1.7-cp311-cp311-win_amd64.whl ; python_version == '3.11'",
]
```

Or install a single wheel directly:

```cmd
pip install https://github.com/maedayoshiaki/edsdk-python/releases/download/v0.1.7/edsdk_python-0.1.7-cp313-cp313-win_amd64.whl
```

You still need Canon EDSDK itself: apply for it through Canon's developer
programme for your region, then point this package at the DLLs before
importing:

```cmd
set EDSDK_PYTHON_DLL_DIR=C:\path\to\EDSDK_64\Dll
```

Vendoring a wheel file into your repository (the previous workflow) keeps
working — the release URLs simply serve the same wheel.
````

- [ ] **Step 2: README に保守者向けリリース手順を追加**

`## Exposure control (Av / Tv / ISO)` セクションの直前に以下を挿入:

````markdown
## Releasing (maintainers)

Releases are built locally on a machine that has the Canon SDK under
`dependencies/`, MSVC, `uv`, and an authenticated `gh` CLI. Canon SDK files
are never uploaded — only the wheels containing this project's code.

1. Verify the build on your branch:
   `python scripts/release.py --dry-run`
   (builds Python 3.11/3.12/3.13 wheels and runs the unit tests against each
   wheel in a throwaway venv).
2. Merge to `main`. The released version must match `pyproject.toml` on
   `origin/main` — the script checks this.
3. Create the release: `python scripts/release.py`
   (creates tag `v{version}`, uploads the wheels from `dist/`, and prints the
   consumer `pyproject.toml` snippet).
````

- [ ] **Step 3: ImportError メッセージに SDK 入手先の一文を追加**

`edsdk/__init__.py` の ImportError を以下に変更(52-56 行目):

```python
        raise ImportError(
            "Failed to import edsdk.api. Install Canon EDSDK separately and set "
            "EDSDK_PYTHON_DLL_DIR or CANON_EDSDK_DLL_DIR to the folder containing "
            "EDSDK.dll and EdsImage.dll. The SDK is not bundled with this package: "
            "apply for it through Canon's developer programme for your region."
        ) from exc
```

- [ ] **Step 4: 全テストが通ることを確認**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: PASS(unit 37 + release script 9。カメラ接続時は実機テストも走るが、未接続なら 9 件 skip)

- [ ] **Step 5: コミット**

```powershell
git add README.md edsdk/__init__.py
git commit -m "docs: GitHub Releasesからの導入ガイドとリリース手順を追加、ImportErrorにSDK入手先を明記"
```
