"""Hardware integration tests for low-level property marshalling."""

from __future__ import annotations

import pytest

from edsdk import api
from edsdk.camera_controller import CameraController
from edsdk.constants import DataType, PropID


def _open_or_skip() -> CameraController:
    controller = CameraController(register_property_events=False)
    try:
        return controller.__enter__()
    except Exception as exc:
        if "No cameras connected" in str(exc):
            pytest.skip(f"No camera available: {exc}")
        raise


def test_scalar_property_types_and_bounds():
    controller = _open_or_skip()
    try:
        int_type, int_size = api.GetPropertySize(
            controller._cam, PropID.BatteryLevel, 0
        )
        uint_type, uint_size = api.GetPropertySize(
            controller._cam, PropID.ImageQuality, 0
        )

        assert int_type == DataType.Int32
        assert int_size == 4
        assert isinstance(
            api.GetPropertyData(controller._cam, PropID.BatteryLevel, 0), int
        )

        assert uint_type == DataType.UInt32
        assert uint_size == 4
        assert isinstance(
            api.GetPropertyData(controller._cam, PropID.ImageQuality, 0), int
        )

        style_type, style_size = api.GetPropertySize(
            controller._cam, PropID.PictureStyleDesc, 0
        )
        style = api.GetPropertyData(controller._cam, PropID.PictureStyleDesc, 0)
        assert style_type == DataType.PictureStyleDesc
        assert style_size > 0
        assert {
            "contrast",
            "sharpness",
            "saturation",
            "colorTone",
            "filterEffect",
            "toningEffect",
            "sharpFineness",
            "sharpThreshold",
        } <= style.keys()

        with pytest.raises(OverflowError, match="signed 32-bit range"):
            api.SetPropertyData(
                controller._cam,
                PropID.BatteryLevel,
                0,
                1 << 31,
            )
        with pytest.raises(OverflowError, match="unsigned 32-bit range"):
            api.SetPropertyData(
                controller._cam,
                PropID.ImageQuality,
                0,
                1 << 32,
            )

        with pytest.raises(api.EdsError) as error:
            api.GetPropertySize(controller._cam, 0xDEADBEEF, 0)
        assert str(error.value)
        assert isinstance(error.value.code, int)
    finally:
        controller.close()


def test_string_property_is_bounded_and_decoded():
    controller = _open_or_skip()
    try:
        string_type, string_size = api.GetPropertySize(
            controller._cam, PropID.ProductName, 0
        )
        assert string_type == DataType.String
        assert string_size > 0
        assert isinstance(
            api.GetPropertyData(controller._cam, PropID.ProductName, 0),
            str,
        )
    finally:
        controller.close()
