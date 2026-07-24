"""Protected EDSDK worker process entry point."""

from __future__ import annotations

import base64
import os
import pickle
import threading
from multiprocessing.connection import Client, Connection
from typing import Any, Dict


_ALLOWED_METHODS = frozenset(
    {
        "set_properties",
        "get_properties",
        "get_av",
        "get_tv",
        "get_iso",
        "set_av",
        "set_tv",
        "set_iso",
        "supported_av",
        "supported_tv",
        "supported_iso",
        "list_supported",
        "get_image_quality_code",
        "capture",
        "_arm_triggered_capture",
        "_trigger_mode_set_properties",
        "_trigger_capture_one",
        "_drain_triggered_capture",
        "_disarm_triggered_capture",
        "start_live_view",
        "stop_live_view",
        "grab_live_view_frame",
    }
)


def _serialize_error(exc: BaseException) -> Dict[str, Any]:
    return {
        "name": type(exc).__name__,
        "module": type(exc).__module__,
        "message": str(exc),
        "code": getattr(exc, "code", None),
    }


def _send(
    connection: Connection, lock: threading.Lock, message: Dict[str, Any]
) -> None:
    with lock:
        connection.send(message)


def serve_connection(
    connection: Connection,
    config: Dict[str, Any],
    controller_factory,
    pump_messages,
) -> int:
    """Serve one parent connection; injectable dependencies keep it testable."""
    send_lock = threading.Lock()

    controller = None
    ready = False
    buffered_logs = []

    def send_log(message: str) -> None:
        nonlocal ready
        if not ready:
            buffered_logs.append(str(message))
            return
        try:
            _send(
                connection,
                send_lock,
                {"type": "log", "message": str(message)},
            )
        except (EOFError, OSError, BrokenPipeError):
            pass

    def send_event(event: Dict[str, Any]) -> None:
        try:
            _send(
                connection,
                send_lock,
                {"type": "event", "event": event},
            )
        except (EOFError, OSError, BrokenPipeError):
            pass

    try:
        try:
            controller = controller_factory(
                index=config["index"],
                save_dir=config["save_dir"],
                save_to=config["save_to"],
                auto_capacity=config["auto_capacity"],
                verbose=config["verbose"],
                logger=send_log,
                register_property_events=config["register_property_events"],
                file_pattern=config["file_pattern"],
                seq_start=config["seq_start"],
                protected=False,
            )
            controller.__enter__()
            controller.on_event(send_event)
        except BaseException as exc:
            try:
                _send(
                    connection,
                    send_lock,
                    {"type": "startup_error", "error": _serialize_error(exc)},
                )
            except (EOFError, OSError, BrokenPipeError):
                pass
            return 1

        _send(connection, send_lock, {"type": "ready"})
        ready = True
        for message in buffered_logs:
            send_log(message)
        buffered_logs.clear()

        while True:
            try:
                has_message = connection.poll(0.01)
            except (EOFError, OSError, BrokenPipeError):
                break
            if not has_message:
                pump_messages()
                continue
            try:
                request = connection.recv()
            except (EOFError, OSError, BrokenPipeError):
                break
            if request.get("type") != "request":
                continue

            request_id = str(request.get("id"))
            method = str(request.get("method"))
            if method == "__close__":
                try:
                    controller.close()
                    response = {
                        "type": "response",
                        "id": request_id,
                        "ok": True,
                        "result": None,
                    }
                except BaseException as exc:
                    response = {
                        "type": "response",
                        "id": request_id,
                        "ok": False,
                        "error": _serialize_error(exc),
                    }
                try:
                    _send(connection, send_lock, response)
                except (EOFError, OSError, BrokenPipeError):
                    pass
                break

            if method not in _ALLOWED_METHODS:
                response = {
                    "type": "response",
                    "id": request_id,
                    "ok": False,
                    "error": _serialize_error(
                        ValueError(f"Worker method is not allowed: {method}")
                    ),
                }
            else:
                try:
                    result = getattr(controller, method)(
                        *request.get("args", ()),
                        **request.get("kwargs", {}),
                    )
                    response = {
                        "type": "response",
                        "id": request_id,
                        "ok": True,
                        "result": result,
                    }
                except BaseException as exc:
                    response = {
                        "type": "response",
                        "id": request_id,
                        "ok": False,
                        "error": _serialize_error(exc),
                    }
            try:
                _send(connection, send_lock, response)
            except (EOFError, OSError, BrokenPipeError):
                break
    finally:
        if controller is not None:
            try:
                controller.close()
            except BaseException:
                pass
        try:
            connection.close()
        except Exception:
            pass
    return 0


def main() -> int:
    address = os.environ.pop("EDSDK_PYTHON_WORKER_ADDRESS", "")
    encoded_authkey = os.environ.pop("EDSDK_PYTHON_WORKER_AUTHKEY", "")
    encoded_config = os.environ.pop("EDSDK_PYTHON_WORKER_CONFIG", "")
    if not address or not encoded_authkey or not encoded_config:
        return 2

    authkey = base64.urlsafe_b64decode(encoded_authkey.encode("ascii"))
    config = pickle.loads(base64.urlsafe_b64decode(encoded_config.encode("ascii")))
    connection = Client(address, family="AF_PIPE", authkey=authkey)

    # Import only after IPC is connected. If camera initialization then fails,
    # the parent still receives a structured startup error.
    from edsdk.camera_controller import CameraController, _pump_messages_once

    return serve_connection(
        connection,
        config,
        CameraController,
        _pump_messages_once,
    )


if __name__ == "__main__":
    raise SystemExit(main())
