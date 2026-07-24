"""Hardware integration tests for host image transfers.

Requires a Canon camera connected over USB. The tests temporarily change
ImageQuality, take photos, and restore the original ImageQuality in ``finally``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from edsdk.camera_controller import (
    CameraController,
    _image_quality_includes_raw,
    _image_quality_is_raw_only,
)
from edsdk.constants.properties import ImageQuality, PropID


def _open_or_skip(**kwargs) -> CameraController:
    controller = CameraController(**kwargs)
    try:
        return controller.__enter__()
    except Exception as exc:
        if "No cameras connected" in str(exc):
            pytest.skip(f"No camera available: {exc}")
        raise


def _supported_quality_codes(controller: CameraController) -> tuple[int, int]:
    codes = controller._get_supported_codes(PropID.ImageQuality)
    jpeg = next(
        (code for code in codes if not _image_quality_includes_raw(code)),
        None,
    )
    raw_jpeg = next(
        (
            code
            for code in codes
            if _image_quality_includes_raw(code)
            and not _image_quality_is_raw_only(code)
        ),
        None,
    )
    if jpeg is None:
        pytest.skip("Camera reports no JPEG-only ImageQuality")
    if raw_jpeg is None:
        pytest.skip("Camera reports no RAW+JPEG ImageQuality")
    return jpeg, raw_jpeg


def _assert_transferred(paths: list[str], expected: int) -> None:
    assert len(paths) == expected
    assert len(set(paths)) == expected
    for path in paths:
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 0


def test_direct_jpeg_and_raw_jpeg_transfers(tmp_path):
    controller = _open_or_skip(
        save_dir=str(tmp_path),
        register_property_events=False,
    )
    original_quality = controller.get_image_quality_code()
    try:
        jpeg, raw_jpeg = _supported_quality_codes(controller)

        controller.set_properties(image_quality=jpeg)
        assert controller.get_image_quality_code() == jpeg
        jpeg_paths = controller.capture(timeout=20.0)
        _assert_transferred(jpeg_paths, 1)

        controller.set_properties(image_quality=raw_jpeg)
        assert controller.get_image_quality_code() == raw_jpeg
        raw_jpeg_paths = controller.capture(timeout=30.0)
        _assert_transferred(raw_jpeg_paths, 2)
        assert any(
            Path(path).suffix.lower() in {".cr2", ".cr3"} for path in raw_jpeg_paths
        )
        assert any(
            Path(path).suffix.lower() not in {".cr2", ".cr3"} for path in raw_jpeg_paths
        )
    finally:
        try:
            controller.set_properties(image_quality=original_quality, validate=False)
            assert controller.get_image_quality_code() == original_quality
        finally:
            controller.close()

    assert list(tmp_path.glob("*.part")) == []


def test_protected_jpeg_transfer(tmp_path):
    controller = _open_or_skip(
        save_dir=str(tmp_path),
        register_property_events=False,
        protected=True,
        worker_start_timeout=20.0,
        worker_shutdown_timeout=10.0,
    )
    original_quality = controller.get_image_quality_code()
    try:
        supported = controller.list_supported()["ImageQuality"]
        jpeg_name = next(
            (
                name
                for name in supported
                if name in ImageQuality.__members__
                and not _image_quality_includes_raw(int(ImageQuality[name]))
            ),
            None,
        )
        if jpeg_name is None:
            pytest.skip("Camera reports no known JPEG-only ImageQuality")

        controller.set_properties(image_quality=jpeg_name)
        paths = controller.capture(timeout=20.0)
        _assert_transferred(paths, 1)
    finally:
        try:
            controller.set_properties(image_quality=original_quality, validate=False)
            assert controller.get_image_quality_code() == original_quality
        finally:
            controller.close()

    assert list(tmp_path.glob("*.part")) == []
