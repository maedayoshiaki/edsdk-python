"""Unit tests for scripts/release.py pure helpers (no camera/SDK required)."""

import sys
import zipfile
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.8-3.10
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import release  # noqa: E402


class TestOptionalDependencies:
    @staticmethod
    def _project():
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]

    def test_examples_extra_includes_capture_numpy_decoder(self):
        examples = self._project()["optional-dependencies"]["examples"]
        assert any(requirement.startswith("imageio>=") for requirement in examples)

    def test_python_310_dev_extra_includes_tomllib_backport(self):
        dev = self._project()["optional-dependencies"]["dev"]
        assert any(
            requirement.startswith("tomli>=")
            and "python_version < '3.11'" in requirement
            for requirement in dev
        )


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
            assert (
                release.wheel_url("maedayoshiaki/edsdk-python", "0.1.7", python)
                in notes
            )
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


class TestInvalidPythonValues:
    def test_all_valid(self):
        assert release.invalid_python_values(["3.11", "3.12"]) == []

    def test_some_invalid(self):
        # "3.9" is valid in format even though it's not in DEFAULT_PYTHONS.
        assert release.invalid_python_values(["311", "three", "3.9"]) == [
            "311",
            "three",
        ]


class TestForbiddenWheelEntries:
    def test_clean_wheel_reports_nothing(self, tmp_path):
        wheel = tmp_path / "clean.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("edsdk/api.pyd", b"")
            zf.writestr("edsdk/__init__.py", b"")
        assert release.forbidden_wheel_entries(wheel) == []

    def test_dll_entries_reported_case_insensitively(self, tmp_path):
        wheel = tmp_path / "dirty.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("edsdk/EDSDK.dll", b"")
            zf.writestr("edsdk/EdsImage.dll", b"")
            zf.writestr("edsdk/eDsDk.DLL", b"")
        entries = release.forbidden_wheel_entries(wheel)
        assert "edsdk/EDSDK.dll" in entries
        assert "edsdk/EdsImage.dll" in entries
        assert "edsdk/eDsDk.DLL" in entries
        assert len(entries) == 3

    def test_non_dll_canon_basenames_reported(self, tmp_path):
        # Non-.dll Canon SDK files (e.g. accidentally bundled headers/libs)
        # must still be caught by the basename check.
        wheel = tmp_path / "dirty.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("edsdk/EDSDK.lib", b"")
            zf.writestr("edsdk/EDSDK.h", b"")
        entries = release.forbidden_wheel_entries(wheel)
        assert "edsdk/EDSDK.lib" in entries
        assert "edsdk/EDSDK.h" in entries

    def test_project_source_files_not_misflagged(self, tmp_path):
        # edsdk_python.cpp / edsdk_utils.h etc. merely start with "edsdk" but
        # are this project's own source files, not Canon SDK binaries.
        wheel = tmp_path / "clean.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("edsdk/edsdk_python.cpp", b"")
            zf.writestr("edsdk/edsdk_python.h", b"")
            zf.writestr("edsdk/edsdk_utils.cpp", b"")
            zf.writestr("edsdk/edsdk_utils.h", b"")
        assert release.forbidden_wheel_entries(wheel) == []
