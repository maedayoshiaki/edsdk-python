"""Hardware integration tests for the low-level image APIs."""

from __future__ import annotations

from pathlib import Path

import pytest

from edsdk import api
from edsdk.camera_controller import CameraController, _image_quality_includes_raw
from edsdk.constants import (
    Access,
    FileCreateDisposition,
    ImageSource,
    ObjectEvent,
    PropID,
    SaveTo,
    StorageType,
    TargetImageType,
)


def _open_or_skip(**kwargs) -> CameraController:
    controller = CameraController(**kwargs)
    try:
        return controller.__enter__()
    except Exception as exc:
        if "No cameras connected" in str(exc):
            pytest.skip(f"No camera available: {exc}")
        raise


def _jpeg_quality_or_skip(controller: CameraController) -> int:
    quality = next(
        (
            code
            for code in controller._get_supported_codes(PropID.ImageQuality)
            if not _image_quality_includes_raw(code)
        ),
        None,
    )
    if quality is None:
        pytest.skip("Camera reports no JPEG-only ImageQuality")
    return quality


def _has_card_storage(controller: CameraController) -> bool:
    for index in range(api.GetChildCount(controller._cam)):
        volume = api.GetChildAtIndex(controller._cam, index)
        try:
            info = api.GetVolumeInfo(volume)
            if info["storageType"] != StorageType.Non and info["maxCapacity"] > 0:
                return True
        finally:
            del volume
    return False


def test_get_image_writes_rgb_to_supplied_stream(tmp_path):
    controller = _open_or_skip(
        save_dir=str(tmp_path),
        register_property_events=False,
    )
    original_quality = controller.get_image_quality_code()
    try:
        controller.set_properties(image_quality=_jpeg_quality_or_skip(controller))
        [jpeg_path] = controller.capture(timeout=20.0)

        source_stream = api.CreateFileStream(
            str(Path(jpeg_path)),
            FileCreateDisposition.OpenExisting,
            Access.Read,
        )
        image = api.CreateImageRef(source_stream)
        image_info = api.GetImageInfo(image, ImageSource.FullView)
        output_stream = api.CreateMemoryStream(0)

        result = api.GetImage(
            image,
            ImageSource.FullView,
            TargetImageType.RGB,
            image_info["effectiveRect"],
            {
                "width": image_info["width"],
                "height": image_info["height"],
            },
            output_stream,
        )

        assert result is None
        assert api.GetLength(output_stream) == (
            image_info["width"] * image_info["height"] * 3
        )
        del output_stream, image, source_stream
    finally:
        try:
            controller.set_properties(image_quality=original_quality, validate=False)
        finally:
            controller.close()


def test_download_thumbnail_writes_to_supplied_stream(tmp_path):
    controller = _open_or_skip(
        save_dir=str(tmp_path),
        save_to=SaveTo.Both,
        register_property_events=False,
    )
    original_quality = controller.get_image_quality_code()
    retained_items = []

    def retain_transferred_item(event, item):
        if event == ObjectEvent.DirItemRequestTransfer:
            retained_items.append(item)
        return 0

    controller.on_object(retain_transferred_item)
    try:
        if not _has_card_storage(controller):
            pytest.skip("Camera reports no writable card storage")

        controller.set_properties(image_quality=_jpeg_quality_or_skip(controller))
        controller.capture(timeout=20.0)
        assert len(retained_items) == 1

        output_stream = api.CreateMemoryStream(0)
        result = api.DownloadThumbnail(retained_items[0], output_stream)

        assert result is None
        assert api.GetLength(output_stream) > 0
        del output_stream
        api.DeleteDirectoryItem(retained_items.pop())
    finally:
        for item in retained_items:
            try:
                api.DeleteDirectoryItem(item)
            except Exception:
                pass
        try:
            controller.set_properties(image_quality=original_quality, validate=False)
        finally:
            controller.close()
