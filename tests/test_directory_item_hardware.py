"""Hardware test for successful DirectoryItem deletion and invalidation.

The test runs only when the camera reports writable card storage. It captures a
new JPEG to both host and camera, deletes only that newly captured camera item,
and never touches pre-existing card contents.
"""

from __future__ import annotations

import gc

import pytest

from edsdk import api
from edsdk.camera_controller import (
    CameraController,
    _image_quality_includes_raw,
)
from edsdk.constants import ObjectEvent, PropID, SaveTo, StorageType


def _open_or_skip(**kwargs) -> CameraController:
    controller = CameraController(**kwargs)
    try:
        return controller.__enter__()
    except Exception as exc:
        if "No cameras connected" in str(exc):
            pytest.skip(f"No camera available: {exc}")
        raise


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


def test_deleted_directory_item_is_invalidated(tmp_path):
    controller = _open_or_skip(
        save_dir=str(tmp_path),
        save_to=SaveTo.Both,
        register_property_events=False,
    )
    retained_items = []
    original_quality = controller.get_image_quality_code()

    def retain_transferred_item(event, item):
        if event == ObjectEvent.DirItemRequestTransfer:
            retained_items.append(item)
        return 0

    controller.on_object(retain_transferred_item)
    try:
        if not _has_card_storage(controller):
            pytest.skip("Camera reports no writable card storage")

        jpeg = next(
            (
                code
                for code in controller._get_supported_codes(PropID.ImageQuality)
                if not _image_quality_includes_raw(code)
            ),
            None,
        )
        if jpeg is None:
            pytest.skip("Camera reports no JPEG-only ImageQuality")

        controller.set_properties(image_quality=jpeg)
        paths = controller.capture(timeout=20.0)
        assert len(paths) == 1
        assert len(retained_items) == 1

        item = retained_items[0]
        api.DeleteDirectoryItem(item)
        retained_items.clear()

        with pytest.raises(ValueError, match="already been released"):
            api.GetDirectoryItemInfo(item)
        with pytest.raises(ValueError, match="already been released"):
            api.DeleteDirectoryItem(item)

        del item
        gc.collect()
    finally:
        for item in retained_items:
            try:
                api.DeleteDirectoryItem(item)
            except Exception:
                pass
        try:
            controller.set_properties(
                image_quality=original_quality,
                validate=False,
            )
        finally:
            controller.close()
