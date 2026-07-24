"""CameraController lifecycle tests using a fake EDSDK backend."""

from __future__ import annotations

import pytest

import edsdk.camera_controller as camera_module
from edsdk.camera_controller import CameraCleanupError, CameraController


def _install_fake_sdk(monkeypatch, *, setup_failure=None, cleanup_failures=()):
    calls = []
    camera = object()

    def record(name, result=None):
        def fake(*args, **kwargs):
            calls.append(name)
            if name == setup_failure or name in cleanup_failures:
                raise RuntimeError(f"{name} failed")
            return result

        return fake

    monkeypatch.setattr(camera_module.edsdk, "InitializeSDK", record("initialize"))
    monkeypatch.setattr(
        camera_module.edsdk, "GetCameraList", record("get_camera_list", object())
    )
    monkeypatch.setattr(
        camera_module.edsdk, "GetChildCount", record("get_child_count", 1)
    )
    monkeypatch.setattr(
        camera_module.edsdk, "GetChildAtIndex", record("get_child", camera)
    )
    monkeypatch.setattr(camera_module.edsdk, "OpenSession", record("open_session"))
    monkeypatch.setattr(
        camera_module.edsdk, "SetObjectEventHandler", record("object_handler")
    )
    monkeypatch.setattr(
        camera_module.edsdk, "SetPropertyEventHandler", record("property_handler")
    )
    monkeypatch.setattr(camera_module.edsdk, "SetPropertyData", record("set_property"))
    monkeypatch.setattr(camera_module.edsdk, "SetCapacity", record("set_capacity"))
    monkeypatch.setattr(camera_module.edsdk, "CloseSession", record("close_session"))
    monkeypatch.setattr(camera_module.edsdk, "TerminateSDK", record("terminate_sdk"))
    return calls


def test_close_is_ordered_and_idempotent(monkeypatch):
    calls = _install_fake_sdk(monkeypatch)
    controller = CameraController()

    assert controller.__enter__() is controller
    controller.close()
    controller.close()

    assert calls.count("close_session") == 1
    assert calls.count("terminate_sdk") == 1
    assert calls.index("close_session") < calls.index("terminate_sdk")
    assert controller._cam is None
    assert not controller._session_open
    assert not controller._sdk_initialized
    assert not controller._atexit_registered


def test_enter_failure_after_open_closes_session_and_sdk(monkeypatch):
    calls = _install_fake_sdk(monkeypatch, setup_failure="set_property")
    controller = CameraController()

    with pytest.raises(RuntimeError, match="set_property failed"):
        controller.__enter__()

    assert "open_session" in calls
    assert calls.index("close_session") < calls.index("terminate_sdk")
    assert controller._cam is None
    assert not controller._entered


@pytest.mark.parametrize(
    ("failure", "session_was_open"),
    [
        ("get_camera_list", False),
        ("get_child_count", False),
        ("get_child", False),
        ("open_session", False),
        ("object_handler", True),
        ("set_capacity", True),
    ],
)
def test_each_open_stage_rolls_back_completed_resources(
    monkeypatch, failure, session_was_open
):
    calls = _install_fake_sdk(monkeypatch, setup_failure=failure)

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        CameraController().__enter__()

    assert calls.count("terminate_sdk") == 1
    assert calls.count("close_session") == int(session_was_open)


def test_keyboard_interrupt_inside_context_still_closes(monkeypatch):
    calls = _install_fake_sdk(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        with CameraController():
            raise KeyboardInterrupt

    assert calls.count("close_session") == 1
    assert calls.count("terminate_sdk") == 1


def test_all_cleanup_stages_run_before_error_is_raised(monkeypatch):
    calls = _install_fake_sdk(
        monkeypatch,
        cleanup_failures={"close_session", "terminate_sdk"},
    )
    controller = CameraController().__enter__()

    with pytest.raises(CameraCleanupError) as captured:
        controller.close()

    assert [stage for stage, _ in captured.value.errors] == [
        "close_session",
        "terminate_sdk",
    ]
    assert calls.index("close_session") < calls.index("terminate_sdk")
    monkeypatch.setattr(
        camera_module.edsdk,
        "TerminateSDK",
        lambda: calls.append("terminate_sdk"),
    )
    controller.close()
    assert calls.count("terminate_sdk") == 2


def test_live_view_is_stopped_before_session_close(monkeypatch):
    calls = _install_fake_sdk(monkeypatch)
    controller = CameraController().__enter__()
    controller._live_view_on = True

    controller.close()

    close_index = calls.index("close_session")
    assert calls[close_index - 2 : close_index] == ["set_property", "set_property"]


def test_serializable_event_reaches_callback_and_async_path(monkeypatch):
    _install_fake_sdk(monkeypatch)
    controller = CameraController()
    received = []
    controller.on_event(received.append)

    event = {"kind": "object", "event": "DirItemCreated", "path": "image.jpg"}
    controller._enqueue_async_event(event)

    assert received == [event]


def test_legacy_native_callbacks_are_rejected_in_protected_mode():
    controller = CameraController(protected=True)

    with pytest.raises(NotImplementedError, match="on_event"):
        controller.on_object(lambda *_args: 0)
    with pytest.raises(NotImplementedError, match="on_event"):
        controller.on_property(lambda *_args: 0)
