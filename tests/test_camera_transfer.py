"""Camera transfer tests using a fake EDSDK backend."""

from __future__ import annotations

import multiprocessing
import sys
import threading
import types
from pathlib import Path
from typing import Any, cast

import pytest

import edsdk.camera_controller as camera_module
from edsdk._camera_worker import serve_connection
from edsdk.camera_controller import CameraController, _save_directory_item
from edsdk.constants.properties import ImageQuality, SaveTo


def _direct_controller(*, save_to: SaveTo = SaveTo.Host) -> CameraController:
    controller = CameraController(save_to=save_to)
    controller._cam = cast(Any, object())
    return controller


def _install_capture_backend(monkeypatch, quality: ImageQuality):
    sent = []
    monkeypatch.setattr(
        camera_module.edsdk,
        "GetPropertyData",
        lambda *_args, **_kwargs: int(quality),
    )
    monkeypatch.setattr(
        camera_module.edsdk,
        "SendCommand",
        lambda *args, **_kwargs: sent.append(args),
    )
    return sent


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"shots": 0}, "shots"),
        ({"shots": -1}, "shots"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": float("inf")}, "timeout"),
        ({"interval": -0.1}, "interval"),
        ({"interval": float("nan")}, "interval"),
        ({"retry": -1}, "retry"),
        ({"retry_delay": -0.1}, "retry_delay"),
        ({"retry_delay": float("inf")}, "retry_delay"),
        ({"retry": 1}, "retry_on_timeout"),
    ],
)
def test_capture_validates_transfer_arguments(monkeypatch, kwargs, message):
    controller = _direct_controller()
    monkeypatch.setattr(camera_module.edsdk, "SendCommand", lambda *_args: None)

    with pytest.raises(ValueError, match=message):
        controller.capture(**kwargs)


def test_capture_requires_host_destination():
    controller = _direct_controller(save_to=SaveTo.Camera)

    with pytest.raises(RuntimeError, match="SaveTo.Host"):
        controller.capture()


def test_enter_prepares_save_directory(monkeypatch, tmp_path):
    save_dir = tmp_path / "nested" / "captures"
    controller = CameraController(save_dir=str(save_dir), protected=True)

    def enter_protected():
        controller._entered = True
        return controller

    monkeypatch.setattr(controller, "_enter_protected", enter_protected)

    assert controller.__enter__() is controller
    assert save_dir.is_dir()


@pytest.mark.parametrize(
    ("quality", "events"),
    [
        (ImageQuality.LJF, ["image.jpg"]),
        (ImageQuality.LR, ["image.cr3"]),
        (ImageQuality.LRLJF, ["image.cr3", "image.jpg"]),
    ],
)
def test_capture_waits_for_every_file_from_one_shot(monkeypatch, quality, events):
    controller = _direct_controller()
    sent = _install_capture_backend(monkeypatch, quality)
    pending = list(events)

    def pump():
        if pending:
            controller._saved_paths.append(pending.pop(0))

    monkeypatch.setattr(camera_module, "_pump_messages_once", pump)

    assert controller.capture(timeout=0.2) == events
    assert len(sent) == 1


def test_partial_raw_jpeg_transfer_is_not_retried(monkeypatch):
    controller = _direct_controller()
    sent = _install_capture_backend(monkeypatch, ImageQuality.LRLJF)
    delivered = False

    def pump():
        nonlocal delivered
        if not delivered:
            delivered = True
            controller._saved_paths.append("image.cr3")

    monkeypatch.setattr(camera_module, "_pump_messages_once", pump)

    with pytest.raises(TimeoutError, match=r"expected 2, received 1"):
        controller.capture(
            timeout=0.02,
            retry=1,
            retry_delay=0.02,
            retry_on_timeout=True,
        )
    assert len(sent) == 1


def test_retry_delay_accepts_late_original_transfer(monkeypatch):
    controller = _direct_controller()
    sent = _install_capture_backend(monkeypatch, ImageQuality.LJF)
    pumps = 0

    def pump():
        nonlocal pumps
        pumps += 1
        if pumps == 2:
            controller._saved_paths.append("late.jpg")

    monkeypatch.setattr(camera_module, "_pump_messages_once", pump)

    assert controller.capture(
        timeout=0.001,
        retry=1,
        retry_delay=0.05,
        retry_on_timeout=True,
    ) == ["late.jpg"]
    assert len(sent) == 1


def test_opted_in_timeout_retry_can_send_second_shutter_command(monkeypatch):
    controller = _direct_controller()
    sent = _install_capture_backend(monkeypatch, ImageQuality.LJF)

    def pump():
        if len(sent) == 2 and not controller._saved_paths:
            controller._saved_paths.append("retry.jpg")

    monkeypatch.setattr(camera_module, "_pump_messages_once", pump)

    assert controller.capture(
        timeout=0.001,
        retry=1,
        retry_delay=0.001,
        retry_on_timeout=True,
    ) == ["retry.jpg"]
    assert len(sent) == 2


class _FakeStream:
    def __init__(self, path: str) -> None:
        self.path = Path(path)


def test_save_directory_item_replaces_atomically(monkeypatch, tmp_path):
    destination = tmp_path / "image.jpg"
    destination.write_bytes(b"old")
    completed = []
    cancelled = []

    monkeypatch.setattr(
        camera_module.edsdk,
        "GetDirectoryItemInfo",
        lambda _handle: {"szFileName": "image.jpg", "size": 3},
    )

    def create(path, *_args):
        stream = _FakeStream(path)
        stream.path.write_bytes(b"")
        return stream

    def download(_handle, _size, stream):
        stream.path.write_bytes(b"new")

    monkeypatch.setattr(camera_module.edsdk, "CreateFileStream", create)
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

    result = _save_directory_item(object(), str(tmp_path))

    assert result == str(destination)
    assert destination.read_bytes() == b"new"
    assert completed == [True]
    assert cancelled == []
    assert list(tmp_path.glob("*.part")) == []


def test_save_failure_cancels_and_removes_partial_file(monkeypatch, tmp_path):
    destination = tmp_path / "image.jpg"
    destination.write_bytes(b"old")
    cancelled = []

    monkeypatch.setattr(
        camera_module.edsdk,
        "GetDirectoryItemInfo",
        lambda _handle: {"szFileName": "image.jpg", "size": 3},
    )

    def create(path, *_args):
        stream = _FakeStream(path)
        stream.path.write_bytes(b"")
        return stream

    def download(_handle, _size, stream):
        stream.path.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(camera_module.edsdk, "CreateFileStream", create)
    monkeypatch.setattr(camera_module.edsdk, "Download", download)
    monkeypatch.setattr(camera_module.edsdk, "DownloadComplete", lambda _handle: None)
    monkeypatch.setattr(
        camera_module.edsdk,
        "DownloadCancel",
        lambda _handle: cancelled.append(True),
    )

    with pytest.raises(OSError, match="disk full"):
        _save_directory_item(object(), str(tmp_path))

    assert destination.read_bytes() == b"old"
    assert cancelled == [True]
    assert list(tmp_path.glob("*.part")) == []


def test_callback_save_error_is_raised_by_waiter(monkeypatch):
    controller = _direct_controller()
    monkeypatch.setattr(
        camera_module.edsdk,
        "GetDirectoryItemInfo",
        lambda _handle: {"szFileName": "image.jpg", "size": 3},
    )
    monkeypatch.setattr(
        camera_module,
        "_save_directory_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("read-only destination")
        ),
    )
    monkeypatch.setattr(camera_module, "_pump_messages_once", lambda: None)

    result = controller._on_object_event(
        camera_module.ObjectEvent.DirItemRequestTransfer, object()
    )

    assert result != 0
    with pytest.raises(PermissionError, match="read-only destination"):
        controller._wait_for_transfer(
            0.02,
            start_count=0,
            expected_count=1,
        )


def test_callback_timeout_error_is_not_treated_as_retryable(monkeypatch):
    controller = _direct_controller()
    sent = _install_capture_backend(monkeypatch, ImageQuality.LJF)

    def pump():
        controller._transfer_error = TimeoutError("filesystem timeout")

    monkeypatch.setattr(camera_module, "_pump_messages_once", pump)

    with pytest.raises(TimeoutError, match="filesystem timeout"):
        controller.capture(
            timeout=0.02,
            retry=1,
            retry_delay=0.02,
            retry_on_timeout=True,
        )
    assert len(sent) == 1


def test_protected_capture_forwards_retry_opt_in():
    calls = []

    class Worker:
        def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return ["worker.jpg"]

    controller = CameraController(protected=True)
    controller._entered = True
    controller._worker = Worker()

    assert controller.capture(
        retry=1,
        retry_on_timeout=True,
    ) == ["worker.jpg"]
    assert calls[0][0][0] == "capture"
    assert calls[0][1]["retry_on_timeout"] is True


def test_worker_forwards_retry_opt_in_to_direct_controller():
    parent, worker = multiprocessing.Pipe(duplex=True)

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def on_event(self, _callback):
            return None

        def capture(self, **kwargs):
            return kwargs["retry_on_timeout"]

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
            "id": "capture",
            "method": "capture",
            "args": (),
            "kwargs": {"retry_on_timeout": True},
        }
    )
    response = parent.recv()
    assert response["ok"] is True
    assert response["result"] is True

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
    assert not thread.is_alive()
    parent.close()


def test_capture_bytes_forwards_retry_opt_in(monkeypatch):
    controller = _direct_controller()
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(controller, "capture", capture)

    assert (
        controller.capture_bytes(
            retry=1,
            retry_on_timeout=True,
        )
        == []
    )
    assert calls[0]["retry_on_timeout"] is True


def test_capture_numpy_forwards_retry_opt_in(monkeypatch):
    controller = _direct_controller()
    calls = []

    imageio = types.ModuleType("imageio")
    imageio.__path__ = []
    imageio_v3 = types.ModuleType("imageio.v3")
    imageio_v3.imread = lambda _data: None
    monkeypatch.setitem(sys.modules, "imageio", imageio)
    monkeypatch.setitem(sys.modules, "imageio.v3", imageio_v3)
    monkeypatch.setattr(
        controller, "get_image_quality_code", lambda: int(ImageQuality.LJF)
    )

    def capture(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(controller, "capture", capture)

    assert (
        controller.capture_numpy(
            retry=1,
            retry_on_timeout=True,
        )
        == []
    )
    assert calls[0]["retry_on_timeout"] is True


def test_cli_forwards_retry_opt_in(monkeypatch, tmp_path):
    from examples import capture_cli

    captures = []

    class FakeCamera:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def set_properties(self, **_kwargs):
            return None

        def get_properties(self):
            return {}

        def capture(self, **kwargs):
            captures.append(kwargs)
            return []

    monkeypatch.setattr(capture_cli, "CameraController", FakeCamera)

    assert (
        capture_cli.main(
            [
                "--save-dir",
                str(tmp_path),
                "--retry",
                "1",
                "--retry-on-timeout",
            ]
        )
        == 0
    )
    assert captures[0]["retry_on_timeout"] is True
