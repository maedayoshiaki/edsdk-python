"""Protected worker transport and orphan-cleanup tests."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from edsdk._camera_ipc import ProtectedWorkerClient
from edsdk._camera_worker import serve_connection
from edsdk.camera_controller import CameraWorkerTimeoutError


def _client(connection, *, events=None, shutdown_timeout=0.2):
    client = ProtectedWorkerClient(
        config={},
        event_handler=(events if events is not None else []).append,
        log_handler=lambda _message: None,
        start_timeout=0.2,
        shutdown_timeout=shutdown_timeout,
    )
    client._connection = connection
    client._receiver = threading.Thread(target=client._receive_loop, daemon=True)
    client._receiver.start()
    return client


def test_rpc_response_and_event_are_demultiplexed():
    parent, worker = multiprocessing.Pipe(duplex=True)
    events = []
    client = _client(parent, events=events)

    def fake_worker():
        request = worker.recv()
        worker.send(
            {
                "type": "event",
                "event": {"kind": "property", "event": "Changed", "param": 1},
            }
        )
        worker.send(
            {
                "type": "response",
                "id": request["id"],
                "ok": True,
                "result": 42,
            }
        )

    thread = threading.Thread(target=fake_worker)
    thread.start()
    assert client.call("get_image_quality_code") == 42
    thread.join(timeout=1)

    assert events == [{"kind": "property", "event": "Changed", "param": 1}]
    client.abandon()
    worker.close()


def test_shutdown_timeout_never_terminates_worker():
    parent, worker = multiprocessing.Pipe(duplex=True)
    client = _client(parent, shutdown_timeout=0.05)

    def unresponsive_worker():
        worker.recv()
        time.sleep(0.2)
        worker.close()

    thread = threading.Thread(target=unresponsive_worker)
    thread.start()
    with pytest.raises(CameraWorkerTimeoutError):
        client.close()
    thread.join(timeout=1)
    assert client._closed


class _MarkerController:
    def __init__(self, **kwargs):
        self.marker = kwargs["file_pattern"]

    def __enter__(self):
        return self

    def on_event(self, _callback):
        return None

    def close(self):
        Path(self.marker).write_text("closed", encoding="utf-8")


def _run_marker_worker(connection, marker):
    config = {
        "index": 0,
        "save_dir": ".",
        "save_to": 0,
        "auto_capacity": False,
        "verbose": False,
        "register_property_events": False,
        "file_pattern": marker,
        "seq_start": 1,
    }
    serve_connection(connection, config, _MarkerController, lambda: None)


def _run_parent_harness(marker, ready):
    context = multiprocessing.get_context("spawn")
    parent, worker = context.Pipe(duplex=True)
    process = context.Process(
        target=_run_marker_worker,
        args=(worker, marker),
        daemon=False,
    )
    process.start()
    worker.close()
    message = parent.recv()
    if message.get("type") == "ready":
        ready.set()
    time.sleep(60)


@pytest.mark.skipif(os.name != "nt", reason="Windows TerminateProcess behavior")
def test_worker_cleans_up_after_parent_is_force_terminated(tmp_path):
    marker = tmp_path / "worker-closed.txt"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    parent = context.Process(
        target=_run_parent_harness,
        args=(str(marker), ready),
    )
    parent.start()
    assert ready.wait(timeout=10)

    parent.terminate()
    parent.join(timeout=5)
    assert not parent.is_alive()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.read_text(encoding="utf-8") == "closed"
