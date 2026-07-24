"""EOS hardware tests for triggered one-frame capture.

These tests take real photographs. Deferred-card tests intentionally preserve
the new card files and never delete pre-existing or test-created images.

Run:
    python -m pytest tests/test_triggered_capture_hardware.py -v -s
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from edsdk.camera_controller import (
    CameraController,
    _image_quality_includes_raw,
    _image_quality_is_raw_only,
)
from edsdk.constants.properties import ImageQuality


def _open_or_skip(tmp_path: Path) -> CameraController:
    controller = CameraController(
        protected=True,
        save_dir=str(tmp_path),
        register_property_events=False,
        worker_start_timeout=20.0,
        worker_shutdown_timeout=10.0,
    )
    try:
        return controller.__enter__()
    except Exception as exc:
        if "No cameras connected" in str(exc):
            pytest.skip(f"No camera available: {exc}")
        raise


def _quality_names(controller: CameraController):
    supported = set(controller.list_supported()["ImageQuality"])
    jpeg = next(
        (
            name
            for name, member in ImageQuality.__members__.items()
            if name in supported and not _image_quality_includes_raw(int(member))
        ),
        None,
    )
    raw_jpeg = next(
        (
            name
            for name, member in ImageQuality.__members__.items()
            if name in supported
            and _image_quality_includes_raw(int(member))
            and not _image_quality_is_raw_only(int(member))
        ),
        None,
    )
    if jpeg is None:
        pytest.skip("Camera reports no known JPEG-only quality")
    return jpeg, raw_jpeg


def _assert_jpeg(data: bytes) -> None:
    assert data.startswith(b"\xff\xd8")
    assert data.endswith(b"\xff\xd9")


def _exposure_exif(image):
    # Canon JPEGs keep ExposureTime/ISO in the nested Exif IFD.
    return image.getexif().get_ifd(0x8769)


def test_memory_trigger_settings_and_raw_jpeg(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    controller = _open_or_skip(tmp_path)
    original_quality = controller.get_image_quality_code()
    mode = None
    try:
        jpeg, raw_jpeg = _quality_names(controller)
        controller.set_properties(image_quality=jpeg)

        mode = controller.arm_triggered_capture(
            transfer="memory",
            defaults={"tv": "1/15", "iso": 400},
        )
        first = mode.trigger(wait=True, timeout=30.0)
        assert len(first.assets) == 1
        jpeg_data = first.assets[0].data
        assert jpeg_data is not None
        _assert_jpeg(jpeg_data)
        with Image.open(io.BytesIO(jpeg_data)) as image:
            exif = _exposure_exif(image)
            assert float(exif[33434]) == pytest.approx(1 / 15, rel=0.05)
            assert int(exif[34855]) == 400

        second = mode.trigger(tv="1/30", iso=800, wait=True, timeout=30.0)
        second_data = second.assets[0].data
        assert second_data is not None
        with Image.open(io.BytesIO(second_data)) as image:
            exif = _exposure_exif(image)
            assert float(exif[33434]) == pytest.approx(1 / 30, rel=0.05)
            assert int(exif[34855]) == 800
        mode.disarm()
        mode = None

        if raw_jpeg is not None:
            controller.set_properties(image_quality=raw_jpeg)
            mode = controller.arm_triggered_capture(transfer="memory")
            raw_frame = mode.trigger(wait=True, timeout=60.0)
            assert len(raw_frame.assets) == 2
            assert all(asset.data for asset in raw_frame.assets)
            assert any(
                Path(asset.filename).suffix.lower() in {".cr2", ".cr3"}
                for asset in raw_frame.assets
            )
            mode.disarm()
            mode = None
    finally:
        if mode is not None:
            mode.disarm(leave_on_card=True)
        try:
            controller.set_properties(
                image_quality=original_quality,
                validate=False,
            )
        finally:
            controller.close()

    assert list(tmp_path.iterdir()) == []


def test_deferred_card_ten_triggers_and_drain(tmp_path):
    controller = _open_or_skip(tmp_path)
    original_quality = controller.get_image_quality_code()
    mode = None
    try:
        jpeg, _raw_jpeg = _quality_names(controller)
        controller.set_properties(image_quality=jpeg)
        try:
            mode = controller.arm_triggered_capture(
                transfer="deferred_card",
                max_deferred_frames=10,
            )
        except RuntimeError as exc:
            if "writable memory card" in str(exc):
                pytest.skip(str(exc))
            raise

        receipts = [mode.trigger(wait=True, timeout=30.0) for _index in range(10)]
        assert all(len(frame.assets) == 1 for frame in receipts)
        assert all(frame.assets[0].data is None for frame in receipts)
        assert list(tmp_path.iterdir()) == []

        downloaded = mode.drain(output="bytes")
        assert [frame.trigger_id for frame in downloaded] == [
            frame.trigger_id for frame in receipts
        ]
        for frame in downloaded:
            data = frame.assets[0].data
            assert data is not None
            _assert_jpeg(data)
        mode.disarm()
        mode = None
    finally:
        if mode is not None:
            mode.disarm(leave_on_card=True)
        try:
            controller.set_properties(
                image_quality=original_quality,
                validate=False,
            )
        finally:
            controller.close()
