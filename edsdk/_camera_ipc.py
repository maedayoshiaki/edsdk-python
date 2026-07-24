"""Parent-side IPC client for protected camera sessions."""

from __future__ import annotations

import base64
import os
import pickle
import queue
import secrets
import subprocess
import sys
import threading
import time
import uuid
from multiprocessing.connection import Connection, Listener
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from edsdk.camera_controller import CameraWorkerError, CameraWorkerTimeoutError

if TYPE_CHECKING:
    from edsdk.camera_controller import CameraEvent


_ADDRESS_ENV = "EDSDK_PYTHON_WORKER_ADDRESS"
_AUTHKEY_ENV = "EDSDK_PYTHON_WORKER_AUTHKEY"


def _remote_exception(payload: Dict[str, Any]) -> BaseException:
    message = str(payload.get("message", "Protected camera worker failed"))
    name = str(payload.get("name", ""))
    code = payload.get("code")
    known = {
        "ValueError": ValueError,
        "TypeError": TypeError,
        "TimeoutError": TimeoutError,
        "RuntimeError": RuntimeError,
        "FileNotFoundError": FileNotFoundError,
    }
    exc_type = known.get(name)
    if exc_type is None:
        return CameraWorkerError(message, code=code)
    exc = exc_type(message)
    if code is not None:
        try:
            setattr(exc, "code", code)
        except Exception:
            pass
    return exc


class ProtectedWorkerClient:
    """Own a single authenticated connection to a camera worker."""

    def __init__(
        self,
        *,
        config: Dict[str, Any],
        event_handler: Callable[["CameraEvent"], None],
        log_handler: Callable[[str], None],
        start_timeout: float,
        shutdown_timeout: float,
    ) -> None:
        self._config = config
        self._event_handler = event_handler
        self._log_handler = log_handler
        self._start_timeout = start_timeout
        self._shutdown_timeout = shutdown_timeout
        self._connection: Optional[Connection] = None
        self._listener: Optional[Listener] = None
        self._process: Optional[subprocess.Popen] = None
        self._receiver: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
        self._closed = False

    def start(self) -> None:
        address = rf"\\.\pipe\edsdk-python-{os.getpid()}-{uuid.uuid4().hex}"
        authkey = secrets.token_bytes(32)
        listener = Listener(address, family="AF_PIPE", authkey=authkey)
        self._listener = listener

        env = os.environ.copy()
        env[_ADDRESS_ENV] = address
        env[_AUTHKEY_ENV] = base64.urlsafe_b64encode(authkey).decode("ascii")
        env["EDSDK_PYTHON_WORKER_CONFIG"] = base64.urlsafe_b64encode(
            pickle.dumps(self._config)
        ).decode("ascii")

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        self._process = subprocess.Popen(
            [sys.executable, "-m", "edsdk._camera_worker"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

        accepted: "queue.Queue[Any]" = queue.Queue(maxsize=1)

        def accept_connection() -> None:
            try:
                accepted.put(listener.accept())
            except BaseException as exc:
                accepted.put(exc)

        accept_thread = threading.Thread(
            target=accept_connection,
            name="edsdk-worker-accept",
            daemon=True,
        )
        accept_thread.start()
        started_at = time.monotonic()
        try:
            accepted_value = accepted.get(timeout=self._start_timeout)
        except queue.Empty as exc:
            listener.close()
            self._listener = None
            raise CameraWorkerTimeoutError(
                "Timed out waiting for protected camera worker connection"
            ) from exc
        finally:
            if self._listener is not None:
                self._listener.close()
                self._listener = None

        if isinstance(accepted_value, BaseException):
            raise CameraWorkerError(
                f"Protected camera worker connection failed: {accepted_value}"
            )
        connection = accepted_value
        self._connection = connection

        remaining = max(0.0, self._start_timeout - (time.monotonic() - started_at))
        if not connection.poll(remaining):
            raise CameraWorkerTimeoutError(
                "Timed out while protected camera worker opened the camera"
            )
        try:
            startup = connection.recv()
        except (EOFError, OSError) as exc:
            raise CameraWorkerError(
                "Protected camera worker exited during startup"
            ) from exc
        if startup.get("type") == "startup_error":
            raise _remote_exception(startup["error"])
        if startup.get("type") != "ready":
            raise CameraWorkerError(
                f"Unexpected protected worker startup message: {startup!r}"
            )

        self._receiver = threading.Thread(
            target=self._receive_loop,
            name="edsdk-worker-receiver",
            daemon=True,
        )
        self._receiver.start()

    def call(self, method: str, *args, **kwargs):
        return self._request(method, args, kwargs, timeout=None)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._request(
                "__close__",
                (),
                {},
                timeout=self._shutdown_timeout,
            )
        except queue.Empty as exc:
            raise CameraWorkerTimeoutError(
                "Protected camera worker did not close before the shutdown timeout"
            ) from exc
        finally:
            self.abandon()
        process = self._process
        if process is not None:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass

    def abandon(self) -> None:
        """Close IPC handles without ever terminating the worker process."""
        if self._closed:
            return
        self._closed = True
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        self._fail_pending(
            CameraWorkerError("Protected camera worker connection is closed")
        )

    def _request(
        self,
        method: str,
        args: tuple,
        kwargs: Dict[str, Any],
        *,
        timeout: Optional[float],
    ):
        if self._closed or self._connection is None:
            raise CameraWorkerError("Protected camera worker is not connected")
        request_id = uuid.uuid4().hex
        result_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = result_queue
        try:
            with self._send_lock:
                self._connection.send(
                    {
                        "type": "request",
                        "id": request_id,
                        "method": method,
                        "args": args,
                        "kwargs": kwargs,
                    }
                )
            response = result_queue.get(timeout=timeout)
        except queue.Empty:
            raise
        except (EOFError, OSError, BrokenPipeError) as exc:
            raise CameraWorkerError(
                "Protected camera worker disconnected while sending a request"
            ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if response.get("ok"):
            return response.get("result")
        raise _remote_exception(response["error"])

    def _receive_loop(self) -> None:
        connection = self._connection
        if connection is None:
            return
        disconnect_error: BaseException = CameraWorkerError(
            "Protected camera worker disconnected"
        )
        try:
            while not self._closed:
                message = connection.recv()
                message_type = message.get("type")
                if message_type == "event":
                    try:
                        self._event_handler(message["event"])
                    except Exception as exc:
                        self._log_handler(f"Camera event dispatch failed: {exc}")
                    continue
                if message_type == "log":
                    self._log_handler(str(message.get("message", "")))
                    continue
                if message_type != "response":
                    self._log_handler(
                        f"Ignore unknown protected worker message: {message!r}"
                    )
                    continue
                with self._pending_lock:
                    result_queue = self._pending.get(str(message.get("id")))
                if result_queue is not None:
                    result_queue.put(message)
        except (EOFError, OSError, BrokenPipeError) as exc:
            disconnect_error = CameraWorkerError(
                f"Protected camera worker disconnected: {exc}"
            )
        except BaseException as exc:
            disconnect_error = CameraWorkerError(
                f"Protected camera receiver failed: {exc}"
            )
        finally:
            self._fail_pending(disconnect_error)

    def _fail_pending(self, exc: BaseException) -> None:
        payload = {
            "type": "response",
            "ok": False,
            "error": {
                "name": type(exc).__name__,
                "message": str(exc),
                "code": getattr(exc, "code", None),
            },
        }
        with self._pending_lock:
            queues = list(self._pending.values())
        for result_queue in queues:
            try:
                result_queue.put_nowait(payload)
            except queue.Full:
                pass
