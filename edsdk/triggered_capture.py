"""One-trigger/one-frame capture mode.

The public objects in this module are deliberately independent from native
EDSDK handles so they can cross the protected-worker IPC boundary.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    TYPE_CHECKING,
    Union,
)

from edsdk.exposure import resolve_av, resolve_iso, resolve_tv

if TYPE_CHECKING:
    from edsdk.camera_controller import CameraController


class CameraBusyError(RuntimeError):
    """Raised immediately when a trigger-mode operation is already in flight."""


class DeferredBufferFullError(RuntimeError):
    """Raised before capture when the deferred-card frame limit is reached."""


class TriggeredCaptureFaultError(RuntimeError):
    """Raised when event correlation can no longer be continued safely."""


@dataclass(frozen=True)
class CapturedAsset:
    """One file produced by a shutter release."""

    filename: str
    size: int
    data: Optional[bytes] = None
    path: Optional[str] = None


@dataclass(frozen=True)
class CapturedFrame:
    """Serializable result for one accepted trigger."""

    trigger_id: str
    accepted_at: float
    started_at: float
    completed_at: float
    requested: Mapping[str, Any]
    applied: Mapping[str, str]
    assets: Sequence[CapturedAsset]


class CaptureTicket:
    """Handle for an asynchronously executing trigger."""

    def __init__(
        self,
        trigger_id: str,
        accepted_at: float,
        future: "Future[CapturedFrame]",
    ) -> None:
        self.trigger_id = trigger_id
        self.accepted_at = accepted_at
        self._future = future

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: Optional[float] = None) -> CapturedFrame:
        return self._future.result(timeout=timeout)


class TriggeredCaptureMode:
    """Armed, single-flight triggered capture state.

    Protected camera sessions support asynchronous calls from any parent
    thread. Direct sessions intentionally support only synchronous calls on the
    session-owning thread because EDSDK callback delivery is thread-sensitive.
    """

    def __init__(
        self,
        controller: "CameraController",
        *,
        transfer: str,
        supported_codes: Mapping[str, Sequence[int]],
        max_deferred_frames: int,
    ) -> None:
        self._controller = controller
        self.transfer = transfer
        self.max_deferred_frames = max_deferred_frames
        self._supported_codes = {
            name: tuple(int(code) for code in codes)
            for name, codes in supported_codes.items()
        }
        self._lock = threading.RLock()
        self._ready_event = threading.Event()
        self._ready_event.set()
        self._armed = True
        self._faulted = False
        self._active_ticket: Optional[CaptureTicket] = None
        self._deferred_count = 0
        self._ready_callbacks: List[Callable[["TriggeredCaptureMode"], None]] = []
        self._executor: Optional[ThreadPoolExecutor] = None
        if controller.protected:
            self._executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="edsdk-trigger",
            )

    @property
    def ready(self) -> bool:
        return self._armed and not self._faulted and self._ready_event.is_set()

    @property
    def faulted(self) -> bool:
        return self._faulted

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready_event.wait(timeout)

    def on_ready(self, callback: Callable[["TriggeredCaptureMode"], None]) -> None:
        if not callable(callback):
            raise TypeError("ready callback must be callable")
        with self._lock:
            self._ready_callbacks.append(callback)

    def _validate_settings(
        self, settings: Mapping[str, Any], *, nearest: bool = False
    ) -> None:
        if settings.get("av") is not None:
            resolve_av(settings["av"], self._supported_codes["av"], nearest=nearest)
        if settings.get("tv") is not None:
            resolve_tv(settings["tv"], self._supported_codes["tv"], nearest=nearest)
        if settings.get("iso") is not None:
            resolve_iso(settings["iso"], self._supported_codes["iso"], nearest=nearest)

    def _begin_operation(self) -> None:
        owner_thread = getattr(self._controller, "_session_thread_id", None)
        if (
            not self._controller.protected
            and owner_thread is not None
            and threading.get_ident() != owner_thread
        ):
            raise RuntimeError(
                "Direct triggered capture operations must run on the thread "
                "that opened the camera session"
            )
        with self._lock:
            if not self._armed:
                raise RuntimeError("Triggered capture mode is not armed")
            if self._faulted:
                raise TriggeredCaptureFaultError(
                    "Triggered capture mode is faulted; disarm and arm it again"
                )
            if not self._ready_event.is_set():
                raise CameraBusyError("Camera is processing another operation")
            self._ready_event.clear()

    def _finish_operation(self, *, faulted: bool = False) -> None:
        callbacks: List[Callable[["TriggeredCaptureMode"], None]] = []
        with self._lock:
            self._faulted = self._faulted or faulted
            self._active_ticket = None
            if self._armed and not self._faulted:
                self._ready_event.set()
                callbacks = list(self._ready_callbacks)
        for callback in callbacks:
            try:
                callback(self)
            except BaseException:
                pass

    @staticmethod
    def _is_fatal_exception(exc: BaseException) -> bool:
        name = type(exc).__name__
        return name in {
            "CameraWorkerError",
            "CameraWorkerTimeoutError",
            "TriggeredCaptureFaultError",
        }

    def trigger(
        self,
        *,
        av: Any = None,
        tv: Any = None,
        iso: Any = None,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Union[CaptureTicket, CapturedFrame]:
        settings = {"av": av, "tv": tv, "iso": iso}
        self._validate_settings(settings)
        if not self._controller.protected and not wait:
            raise RuntimeError(
                "wait=False requires CameraController(protected=True); "
                "direct sessions must use wait=True on the owning thread"
            )
        with self._lock:
            if (
                self.transfer == "deferred_card"
                and self._deferred_count >= self.max_deferred_frames
            ):
                raise DeferredBufferFullError(
                    "Deferred card buffer is full; drain before triggering again"
                )

        self._begin_operation()
        trigger_id = uuid.uuid4().hex
        accepted_at = time.time()

        def invoke() -> CapturedFrame:
            return self._controller._trigger_capture_one(
                trigger_id=trigger_id,
                accepted_at=accepted_at,
                av=av,
                tv=tv,
                iso=iso,
                timeout=timeout,
            )

        try:
            if self._executor is not None:
                operation_future = self._executor.submit(invoke)
            else:
                operation_future = Future()
                try:
                    operation_future.set_result(invoke())
                except BaseException as exc:
                    operation_future.set_exception(exc)
        except BaseException:
            self._finish_operation()
            raise

        public_future: "Future[CapturedFrame]" = Future()
        ticket = CaptureTicket(trigger_id, accepted_at, public_future)
        with self._lock:
            self._active_ticket = ticket

        def completed(done_future: "Future[CapturedFrame]") -> None:
            try:
                frame = done_future.result()
            except BaseException as exc:
                self._finish_operation(faulted=self._is_fatal_exception(exc))
                public_future.set_exception(exc)
                return
            if self.transfer == "deferred_card":
                with self._lock:
                    self._deferred_count += 1
            self._finish_operation()
            public_future.set_result(frame)

        operation_future.add_done_callback(completed)
        if wait:
            # ``timeout`` is the camera/event timeout passed to the worker.
            # A separate client-side wait timeout is available on ticket.result().
            return ticket.result()
        return ticket

    def set_properties(
        self,
        *,
        av: Any = None,
        tv: Any = None,
        iso: Any = None,
        nearest: bool = False,
    ) -> Mapping[str, str]:
        settings = {"av": av, "tv": tv, "iso": iso}
        self._validate_settings(settings, nearest=nearest)
        self._begin_operation()
        try:
            return self._controller._trigger_mode_set_properties(
                av=av,
                tv=tv,
                iso=iso,
                nearest=nearest,
            )
        finally:
            self._finish_operation()

    def drain(self, *, output: str = "bytes") -> List[CapturedFrame]:
        if self.transfer != "deferred_card":
            raise RuntimeError("drain() is available only for deferred_card mode")
        if output not in {"bytes", "files"}:
            raise ValueError("output must be 'bytes' or 'files'")
        self._begin_operation()
        try:
            frames = self._controller._drain_triggered_capture(output=output)
            with self._lock:
                self._deferred_count = 0
            return frames
        finally:
            self._finish_operation()

    def disarm(
        self,
        *,
        drain_output: Optional[str] = None,
        leave_on_card: bool = False,
    ) -> List[CapturedFrame]:
        owner_thread = getattr(self._controller, "_session_thread_id", None)
        if (
            not self._controller.protected
            and owner_thread is not None
            and threading.get_ident() != owner_thread
        ):
            raise RuntimeError(
                "Direct triggered capture operations must run on the thread "
                "that opened the camera session"
            )
        with self._lock:
            ticket = self._active_ticket
        if ticket is not None and not ticket.done():
            ticket.result()

        drained: List[CapturedFrame] = []
        if drain_output is not None:
            drained = self.drain(output=drain_output)

        with self._lock:
            if not self._armed:
                return drained
            if not self._ready_event.is_set() and not self._faulted:
                raise CameraBusyError("Camera is processing another operation")
            self._ready_event.clear()
        try:
            self._controller._disarm_triggered_capture(leave_on_card=leave_on_card)
        finally:
            with self._lock:
                self._armed = False
                self._active_ticket = None
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None
        return drained

    def __enter__(self) -> "TriggeredCaptureMode":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Never discard deferred captures implicitly.
        self.disarm()


__all__ = [
    "CameraBusyError",
    "DeferredBufferFullError",
    "TriggeredCaptureFaultError",
    "CapturedAsset",
    "CapturedFrame",
    "CaptureTicket",
    "TriggeredCaptureMode",
]
