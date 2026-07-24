"""Unit tests for one-trigger/one-frame capture mode (no camera required)."""

from __future__ import annotations

import multiprocessing
import threading
import time

import pytest

import edsdk.camera_controller as camera_module
from edsdk._camera_worker import serve_connection
from edsdk.camera_controller import (
    CameraController,
    _download_directory_item_bytes,
)
from edsdk.constants import ObjectEvent, PropID
from edsdk.constants.properties import DriveMode, ImageQuality, SaveTo
from edsdk.exposure import ShutterSpeed
from edsdk.triggered_capture import (
    CameraBusyError,
    CapturedAsset,
    CapturedFrame,
    DeferredBufferFullError,
    TriggeredCaptureMode,
)


SUPPORTED = {
    "av": [0x30],
    "tv": [0x58, 0x60],
    "iso": [0x58, 0x60],
}


def _frame(trigger_id: str, accepted_at: float) -> CapturedFrame:
    now = time.time()
    return CapturedFrame(
        trigger_id=trigger_id,
        accepted_at=accepted_at,
        started_at=now,
        completed_at=now,
        requested={},
        applied={"av": "f/5.6", "tv": "1/15", "iso": "ISO 400"},
        assets=(CapturedAsset("image.jpg", 3, data=b"jpg"),),
    )


class _AsyncFakeController:
    protected = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.disarmed = False
        self.settings = {}

    def _trigger_capture_one(self, *, trigger_id, accepted_at, **_kwargs):
        self.started.set()
        assert self.release.wait(timeout=2)
        return _frame(trigger_id, accepted_at)

    def _trigger_mode_set_properties(self, **kwargs):
        self.settings.update(
            {key: value for key, value in kwargs.items() if value is not None}
        )
        return {"av": "f/5.6", "tv": "1/15", "iso": "ISO 400"}

    def _drain_triggered_capture(self, *, output):
        return []

    def _disarm_triggered_capture(self, *, leave_on_card):
        self.disarmed = True


def test_async_trigger_is_single_flight_and_busy_is_immediate():
    controller = _AsyncFakeController()
    mode = TriggeredCaptureMode(
        controller,
        transfer="memory",
        supported_codes=SUPPORTED,
        max_deferred_frames=10,
    )

    ticket = mode.trigger(wait=False)
    assert controller.started.wait(timeout=1)
    started = time.monotonic()
    with pytest.raises(CameraBusyError):
        mode.trigger(wait=False)
    assert time.monotonic() - started < 0.1

    controller.release.set()
    frame = ticket.result(timeout=1)
    assert frame.trigger_id == ticket.trigger_id
    assert mode.wait_ready(timeout=1)
    assert mode.ready
    mode.disarm()
    assert controller.disarmed


def test_trigger_wait_true_and_separate_property_setting():
    controller = _AsyncFakeController()
    controller.release.set()
    mode = TriggeredCaptureMode(
        controller,
        transfer="memory",
        supported_codes=SUPPORTED,
        max_deferred_frames=10,
    )

    applied = mode.set_properties(tv="1/15")
    assert applied["tv"] == "1/15"
    frame = mode.trigger(iso=400, wait=True)
    assert isinstance(frame, CapturedFrame)
    assert mode.ready
    mode.disarm()


def test_deferred_limit_is_rejected_before_async_submission():
    controller = _AsyncFakeController()
    controller.release.set()
    mode = TriggeredCaptureMode(
        controller,
        transfer="deferred_card",
        supported_codes=SUPPORTED,
        max_deferred_frames=1,
    )
    mode.trigger(wait=True)

    with pytest.raises(DeferredBufferFullError):
        mode.trigger(wait=False)
    mode.disarm(leave_on_card=True)


def test_direct_session_rejects_async_trigger():
    controller = _AsyncFakeController()
    controller.protected = False
    mode = TriggeredCaptureMode(
        controller,
        transfer="memory",
        supported_codes=SUPPORTED,
        max_deferred_frames=10,
    )

    with pytest.raises(RuntimeError, match="protected=True"):
        mode.trigger(wait=False)
    mode.disarm()


def test_memory_download_uses_exact_python_buffer(monkeypatch):
    completed = []
    cancelled = []

    monkeypatch.setattr(
        camera_module.edsdk,
        "GetDirectoryItemInfo",
        lambda _handle: {"szFileName": "image.jpg", "size": 4},
    )
    monkeypatch.setattr(
        camera_module.edsdk,
        "CreateMemoryStreamFromPointer",
        lambda buffer: buffer,
    )

    def download(_handle, size, stream):
        assert size == len(stream) == 4
        stream[:] = b"JPEG"

    monkeypatch.setattr(camera_module.edsdk, "Download", download)
    monkeypatch.setattr(
        camera_module.edsdk,
        "DownloadComplete",
        lambda _handle: completed.append(True),
    )
    monkeypatch.setattr(
        camera_module.edsdk,
        "DownloadCancel",
        lambda _handle: cancelled.append(True),
    )

    asset = _download_directory_item_bytes(object())
    assert asset == CapturedAsset("image.jpg", 4, data=b"JPEG")
    assert completed == [True]
    assert cancelled == []


def test_memory_download_cancels_on_failure(monkeypatch):
    cancelled = []
    monkeypatch.setattr(
        camera_module.edsdk,
        "GetDirectoryItemInfo",
        lambda _handle: {"szFileName": "image.jpg", "size": 4},
    )
    monkeypatch.setattr(
        camera_module.edsdk,
        "CreateMemoryStreamFromPointer",
        lambda buffer: buffer,
    )
    monkeypatch.setattr(
        camera_module.edsdk,
        "Download",
        lambda *_args: (_ for _ in ()).throw(OSError("USB failure")),
    )
    monkeypatch.setattr(
        camera_module.edsdk,
        "DownloadCancel",
        lambda _handle: cancelled.append(True),
    )

    with pytest.raises(OSError, match="USB failure"):
        _download_directory_item_bytes(object())
    assert cancelled == [True]


def _armed_controller(transfer: str) -> CameraController:
    controller = CameraController(auto_capacity=False)
    controller._cam = object()
    controller._triggered_armed = True
    controller._triggered_transfer = transfer
    controller._triggered_max_deferred_frames = 10
    controller._current_trigger_settings = lambda: {  # type: ignore[method-assign]
        "av": "f/5.6",
        "tv": "1/15",
        "iso": "ISO 400",
    }
    controller.get_tv = lambda: ShutterSpeed.from_code(0x58)  # type: ignore[method-assign]
    controller.get_image_quality_code = lambda: int(  # type: ignore[method-assign]
        ImageQuality.LJF
    )
    return controller


def test_controller_correlates_memory_event_to_one_trigger(monkeypatch):
    controller = _armed_controller("memory")
    sent = []
    pending = [object()]
    monkeypatch.setattr(
        camera_module.edsdk,
        "SendCommand",
        lambda *args: sent.append(args),
    )
    monkeypatch.setattr(
        camera_module,
        "_download_directory_item_bytes",
        lambda _handle: CapturedAsset("image.jpg", 3, data=b"jpg"),
    )

    def pump():
        if pending:
            controller._on_object_event(
                ObjectEvent.DirItemRequestTransfer, pending.pop()
            )

    monkeypatch.setattr(camera_module, "_pump_messages_once", pump)
    frame = controller._trigger_capture_one(
        trigger_id="one",
        accepted_at=time.time(),
        timeout=0.2,
    )

    assert len(sent) == 1
    assert frame.trigger_id == "one"
    assert frame.assets[0].data == b"jpg"
    assert controller._triggered_active is None


def test_deferred_card_limit_and_ordered_drain(monkeypatch):
    controller = _armed_controller("deferred_card")
    next_handle = []
    shot = 0

    def send(*_args):
        nonlocal shot
        shot += 1
        next_handle.append(f"image-{shot}.jpg")

    def pump():
        if next_handle:
            controller._on_object_event(ObjectEvent.DirItemCreated, next_handle.pop(0))

    monkeypatch.setattr(camera_module.edsdk, "SendCommand", send)
    monkeypatch.setattr(camera_module, "_pump_messages_once", pump)
    monkeypatch.setattr(
        camera_module,
        "_directory_item_asset",
        lambda handle: CapturedAsset(str(handle), 3),
    )
    monkeypatch.setattr(
        camera_module,
        "_download_directory_item_bytes",
        lambda handle: CapturedAsset(str(handle), 3, data=str(handle).encode("ascii")),
    )

    for index in range(10):
        frame = controller._trigger_capture_one(
            trigger_id=str(index),
            accepted_at=time.time(),
            timeout=0.2,
        )
        assert frame.assets[0].data is None

    with pytest.raises(DeferredBufferFullError):
        controller._trigger_capture_one(
            trigger_id="overflow",
            accepted_at=time.time(),
            timeout=0.2,
        )

    drained = controller._drain_triggered_capture(output="bytes")
    assert [frame.trigger_id for frame in drained] == [
        str(index) for index in range(10)
    ]
    assert drained[0].assets[0].data == b"image-1.jpg"
    assert controller._triggered_deferred == []


def test_partial_deferred_drain_retries_only_unfinished_assets(monkeypatch):
    controller = _armed_controller("deferred_card")
    metadata = (
        CapturedAsset("image.cr3", 3),
        CapturedAsset("image.jpg", 3),
    )
    frame = CapturedFrame(
        trigger_id="raw-jpeg",
        accepted_at=time.time(),
        started_at=time.time(),
        completed_at=time.time(),
        requested={},
        applied={},
        assets=metadata,
    )
    controller._triggered_deferred.append(
        camera_module._DeferredCapture(
            frame=frame,
            handles=["raw", "jpeg"],
            downloaded=[None, None],
        )
    )
    calls = []
    fail_jpeg = True

    def download(handle):
        nonlocal fail_jpeg
        calls.append(handle)
        if handle == "jpeg" and fail_jpeg:
            fail_jpeg = False
            raise OSError("temporary USB failure")
        return CapturedAsset(str(handle), 3, data=b"ok")

    monkeypatch.setattr(camera_module, "_download_directory_item_bytes", download)

    with pytest.raises(OSError, match="temporary USB failure"):
        controller._drain_triggered_capture(output="bytes")
    assert calls == ["raw", "jpeg"]
    assert controller._triggered_deferred[0].downloaded[0] is not None

    drained = controller._drain_triggered_capture(output="bytes")
    assert calls == ["raw", "jpeg", "jpeg"]
    assert len(drained) == 1
    assert controller._triggered_deferred == []


def test_arm_disarm_restores_transport_but_not_exposure(monkeypatch):
    controller = CameraController(auto_capacity=False)
    controller._cam = object()
    set_calls = []

    def get_property(_cam, pid, _param):
        if pid == PropID.SaveTo:
            return int(SaveTo.Both)
        if pid == PropID.DriveMode:
            return int(DriveMode.HighSpeedContinuous)
        raise AssertionError(pid)

    monkeypatch.setattr(camera_module.edsdk, "GetPropertyData", get_property)
    monkeypatch.setattr(
        camera_module.edsdk,
        "SetPropertyData",
        lambda _cam, pid, _param, value: set_calls.append((pid, int(value))),
    )

    controller._arm_triggered_capture(
        transfer="memory",
        defaults={},
        max_deferred_frames=10,
    )
    controller._disarm_triggered_capture()

    assert (PropID.SaveTo, int(SaveTo.Host)) in set_calls
    assert (PropID.DriveMode, int(DriveMode.SingleShooting)) in set_calls
    assert set_calls[-2:] == [
        (PropID.DriveMode, int(DriveMode.HighSpeedContinuous)),
        (PropID.SaveTo, int(SaveTo.Both)),
    ]


def test_deferred_arm_rejects_missing_writable_card(monkeypatch):
    controller = CameraController(auto_capacity=False)
    controller._cam = object()
    monkeypatch.setattr(
        camera_module.edsdk,
        "GetPropertyData",
        lambda _cam, pid, _param: (
            int(SaveTo.Host) if pid == PropID.SaveTo else int(DriveMode.SingleShooting)
        ),
    )
    monkeypatch.setattr(camera_module, "_has_writable_card_storage", lambda _cam: False)

    with pytest.raises(RuntimeError, match="writable memory card"):
        controller._arm_triggered_capture(
            transfer="deferred_card",
            defaults={},
            max_deferred_frames=10,
        )


def test_normal_capture_is_rejected_while_armed():
    controller = _armed_controller("memory")
    with pytest.raises(RuntimeError, match="TriggeredCaptureMode"):
        controller.capture()


@pytest.mark.parametrize(
    "operation",
    [
        lambda controller: controller.set_av(5.6),
        lambda controller: controller.set_tv("1/15"),
        lambda controller: controller.set_iso(400),
        lambda controller: controller.set_properties(tv="1/15"),
    ],
)
def test_normal_property_writes_are_rejected_while_armed(operation):
    controller = _armed_controller("memory")
    with pytest.raises(RuntimeError, match="TriggeredCaptureMode"):
        operation(controller)


def test_capture_bytes_default_uses_diskless_trigger_mode(monkeypatch):
    controller = CameraController()
    controller._cam = object()
    disarmed = []

    class Mode:
        def trigger(self, **_kwargs):
            return _frame("capture-bytes", time.time())

        def disarm(self, **_kwargs):
            disarmed.append(True)

    monkeypatch.setattr(
        controller,
        "arm_triggered_capture",
        lambda **_kwargs: Mode(),
    )
    monkeypatch.setattr(
        controller,
        "capture",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy file capture should not run")
        ),
    )

    assert controller.capture_bytes() == [b"jpg"]
    assert disarmed == [True]


def test_worker_allows_triggered_capture_rpc_methods():
    parent, worker = multiprocessing.Pipe(duplex=True)

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def on_event(self, _callback):
            return None

        def _arm_triggered_capture(self, **_kwargs):
            return None

        def _trigger_capture_one(self, *, trigger_id, accepted_at, **_kwargs):
            return _frame(trigger_id, accepted_at)

        def _disarm_triggered_capture(self, **_kwargs):
            return None

        def close(self):
            return None

    config = {
        "index": 0,
        "save_dir": ".",
        "save_to": int(SaveTo.Host),
        "auto_capacity": False,
        "verbose": False,
        "register_property_events": False,
        "file_pattern": None,
        "seq_start": 1,
    }
    thread = threading.Thread(
        target=serve_connection,
        args=(worker, config, FakeController, lambda: None),
    )
    thread.start()
    assert parent.recv()["type"] == "ready"

    parent.send(
        {
            "type": "request",
            "id": "trigger",
            "method": "_trigger_capture_one",
            "args": (),
            "kwargs": {"trigger_id": "rpc", "accepted_at": 1.0},
        }
    )
    response = parent.recv()
    assert response["ok"] is True
    assert response["result"].trigger_id == "rpc"

    parent.send(
        {
            "type": "request",
            "id": "close",
            "method": "__close__",
            "args": (),
            "kwargs": {},
        }
    )
    assert parent.recv()["ok"] is True
    thread.join(timeout=1)
    parent.close()
