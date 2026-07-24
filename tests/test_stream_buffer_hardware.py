"""Hardware integration test for EDSDK streams backed by Python buffers."""

from __future__ import annotations

import pytest

from edsdk.camera_controller import CameraController


def _open_or_skip() -> CameraController:
    controller = CameraController(register_property_events=False)
    try:
        return controller.__enter__()
    except Exception as exc:
        if "No cameras connected" in str(exc):
            pytest.skip(f"No camera available: {exc}")
        raise


def test_live_view_uses_python_owned_buffer():
    controller = _open_or_skip()
    try:
        frame = controller.grab_live_view_frame()
        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame.startswith(b"\xff\xd8")
    finally:
        controller.close()
