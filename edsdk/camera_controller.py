from __future__ import annotations

import atexit
import os
import json
import io
import asyncio
import math
import time
import uuid
import inspect
from enum import IntEnum
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
    TYPE_CHECKING,
    Type,
    TypedDict,
    runtime_checkable,
)

# Only imported for type checking to avoid runtime cost if deps not installed
if TYPE_CHECKING:  # pragma: no cover
    import numpy as np
    from edsdk._camera_ipc import ProtectedWorkerClient


# External SDK imports
import edsdk
from edsdk import (
    Access,
    CameraCommand,
    EdsObject,
    FileCreateDisposition,
    ObjectEvent,
    PropID,
    PropertyEvent,
)
from edsdk.constants.properties import (
    Av as AvTable,
    Tv as TvTable,
    ISOSpeedCamera,
    SaveTo,
    AEMode,
    MeteringMode,
    WhiteBalance,
    ImageQuality,
    DriveMode,
    EvfOutputDevice,
    PropID as _PropIDEnum,
    AFMode,
    EvfAFMode,
)
from edsdk.exposure import (
    Aperture,
    ApertureLike,
    ISOSpeed,
    ISOSpeedLike,
    ShutterSpeed,
    ShutterSpeedLike,
    resolve_av,
    resolve_iso,
    resolve_tv,
)


# Public callback / return type aliases (after imports to satisfy linters)
ObjectCallback = Callable[["ObjectEvent", "EdsObject"], int]
PropertyCallback = Callable[["PropertyEvent", "PropID", int], int]
LiveViewData = Union[bytes, str]


class CameraEvent(TypedDict, total=False):
    """Serializable camera event used by direct and protected controllers."""

    kind: str
    event: Union[str, int]
    path: str
    property: Union[str, int]
    param: int


EventCallback = Callable[[CameraEvent], None]


class CameraCleanupError(RuntimeError):
    """Raised after every cleanup stage has been attempted."""

    def __init__(self, errors: List[Tuple[str, BaseException]]) -> None:
        self.errors = tuple(errors)
        details = "; ".join(f"{stage}: {exc}" for stage, exc in errors)
        super().__init__(f"Camera cleanup failed ({details})")


class CameraWorkerError(RuntimeError):
    """Raised when the protected camera worker cannot complete a request."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class CameraWorkerTimeoutError(CameraWorkerError, TimeoutError):
    """Raised when protected worker startup or shutdown times out."""


@runtime_checkable
class RawProcessor(Protocol):
    """Protocol for RAW image development callbacks.

    Implement a function or callable object with this signature to process
    RAW image data from the camera.

    Args:
        raw_bytes: Binary data of the RAW file (e.g., CR2, CR3).

    Returns:
        Developed image as numpy.ndarray (RGB; uint8/uint16 等を想定).

    Example using rawpy (8bit):
        ```python
        import io
        import rawpy
        import numpy as np

        def develop_raw(raw_bytes: bytes) -> np.ndarray:
            with rawpy.imread(io.BytesIO(raw_bytes)) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    output_bps=8,
                )
            return rgb  # shape: (H, W, 3), dtype=uint8
        ```

    Example using rawpy (16bit):
        ```python
        import io
        import rawpy
        import numpy as np

        def develop_raw_16(raw_bytes: bytes) -> np.ndarray:
            with rawpy.imread(io.BytesIO(raw_bytes)) as raw:
                rgb = raw.postprocess(
                    gamma=(1, 1),
                    output_bps=16,
                )
            return rgb  # dtype=uint16
        ```
    """

    def __call__(self, raw_bytes: bytes) -> "np.ndarray":
        """Process RAW bytes and return developed numpy array (RGB)."""
        ...


def _validate_raw_processor(processor: object) -> None:
    """Validate that processor conforms to RawProcessor protocol.

    Args:
        processor: Object to validate.

    Raises:
        TypeError: If processor is not callable or has wrong signature.
    """
    if not callable(processor):
        raise TypeError(
            "raw_processor must be callable.\n"
            "Expected signature: (raw_bytes: bytes) -> numpy.ndarray\n"
            "See RawProcessor docstring for implementation examples."
        )

    # Check signature if possible
    try:
        sig = inspect.signature(processor)
        params = [
            p
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        # Should have exactly 1 required positional parameter
        if len(params) != 1:
            raise TypeError(
                f"raw_processor must accept exactly 1 required argument (raw_bytes), "
                f"but got {len(params)} required argument(s).\n"
                "Expected signature: (raw_bytes: bytes) -> numpy.ndarray"
            )
    except (ValueError, TypeError):
        # Some built-in callables don't support signature inspection
        pass


# RAW type codes in upper 16 bits of ImageQuality
# 0x0064: RAW (CR2/CR3), 0x0164: MRAW (SRAW1), 0x0264: SRAW (SRAW2), 0x0063: CRAW
_RAW_QUALITY_PREFIXES = frozenset({0x0064, 0x0164, 0x0264, 0x0063})

# File extensions for RAW images (fallback check)
RAW_EXTENSIONS = frozenset(
    {
        ".cr2",
        ".cr3",  # Canon
        ".nef",  # Nikon
        ".arw",  # Sony
        ".dng",  # Adobe DNG
        ".orf",  # Olympus
        ".rw2",  # Panasonic
        ".pef",  # Pentax
        ".srw",  # Samsung
        ".raf",  # Fujifilm
    }
)


# Windows message pumping for EDSDK callbacks
if os.name == "nt":
    try:
        import pythoncom  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        pythoncom = None  # type: ignore
else:  # pragma: no cover - not required outside Windows
    pythoncom = None  # type: ignore


def _pump_messages_once() -> None:
    if pythoncom is not None:
        pythoncom.PumpWaitingMessages()


def _save_directory_item(
    object_handle: EdsObject, save_dir: str, dst_basename: Optional[str] = None
) -> str:
    info = edsdk.GetDirectoryItemInfo(object_handle)
    orig_name = info.get("szFileName") or f"{uuid.uuid4()}.bin"
    filename = dst_basename or orig_name
    # sanitize path separators in provided name
    filename = filename.replace("\\", "_").replace("/", "_")
    dst = os.path.join(save_dir, filename)
    temp_name = f".{filename}.{uuid.uuid4().hex}.part"
    temp_path = os.path.join(save_dir, temp_name)
    completed = False
    out_stream: Optional[EdsObject] = None
    try:
        out_stream = edsdk.CreateFileStream(
            temp_path,
            FileCreateDisposition.CreateAlways,
            Access.ReadWrite,
        )
        edsdk.Download(object_handle, info["size"], out_stream)
        edsdk.DownloadComplete(object_handle)
        completed = True

        # Release the SDK stream before replacing the final path so Windows no
        # longer holds the temporary file open.
        out_stream = None
        os.replace(temp_path, dst)
        return dst
    except BaseException:
        if not completed:
            try:
                edsdk.DownloadCancel(object_handle)
            except BaseException:
                pass
        out_stream = None
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def _image_quality_includes_raw(quality_code: int) -> bool:
    """Check if the ImageQuality setting includes RAW capture.

    Args:
        quality_code: The ImageQuality property value from camera.

    Returns:
        True if RAW (including CRAW/MRAW/SRAW) is part of the capture.
    """
    upper = (quality_code >> 16) & 0xFFFF
    return upper in _RAW_QUALITY_PREFIXES


def _image_quality_is_raw_only(quality_code: int) -> bool:
    """Check if the ImageQuality setting is RAW-only (no JPEG/HEIF).

    RAW-only values have 0xFF0F in lower 16 bits.
    """
    if not _image_quality_includes_raw(quality_code):
        return False
    lower = quality_code & 0xFFFF
    return lower == 0xFF0F


def _expected_files_per_shot(quality_code: int) -> int:
    """Return the host-transfer count expected for one shutter release."""
    if _image_quality_includes_raw(quality_code) and not _image_quality_is_raw_only(
        quality_code
    ):
        return 2
    return 1


def _is_raw_file(path: str) -> bool:
    """Check if the file is a RAW image based on extension."""
    _, ext = os.path.splitext(path)
    return ext.lower() in RAW_EXTENSIONS


class CameraController:
    """
    A small, ergonomic wrapper around edsdk for property management and capture.

    Contract
    - Inputs: av (e.g., 5.6 or "f/5.6"), tv (e.g., "1/125" or 0.5), iso (int or "auto"), save_dir
    - Output: list of saved file paths from captures
    - Error modes: invalid properties -> ValueError, no camera -> RuntimeError, timeouts -> TimeoutError
    - Success: returns list with at least one valid path when capture completes
    """

    def __init__(
        self,
        index: int = 0,
        save_dir: str = ".",
        save_to: SaveTo = SaveTo.Host,
        auto_capacity: bool = True,
        *,
        verbose: bool = False,
        logger: Optional[Callable[[str], None]] = None,
        register_property_events: bool = True,
        file_pattern: Optional[str] = None,
        seq_start: int = 1,
        protected: bool = False,
        worker_start_timeout: float = 10.0,
        worker_shutdown_timeout: float = 5.0,
    ) -> None:
        if worker_start_timeout <= 0:
            raise ValueError("worker_start_timeout must be greater than zero")
        if worker_shutdown_timeout <= 0:
            raise ValueError("worker_shutdown_timeout must be greater than zero")
        self.index = index
        self.save_dir = save_dir
        self.save_to = save_to
        self.auto_capacity = auto_capacity
        self.verbose = verbose
        self._log = logger or (print if verbose else (lambda *_args, **_kw: None))
        self._cam: Optional[EdsObject] = None
        self._saved_paths: List[str] = []
        self._obj_cb: Optional[ObjectCallback] = None
        self._prop_cb: Optional[PropertyCallback] = None
        self._event_cb: Optional[EventCallback] = None
        self._live_view_on: bool = False
        # asyncio event queue support
        self._async_queue: Optional[asyncio.Queue[CameraEvent]] = None
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_pumping: bool = False
        self._register_property_events = register_property_events
        self._file_pattern = file_pattern
        self._seq = int(seq_start)
        # One-shot explicit filename (base name); if set, next capture uses this name
        self._next_filename: Optional[str] = None
        self.protected = bool(protected)
        self.worker_start_timeout = float(worker_start_timeout)
        self.worker_shutdown_timeout = float(worker_shutdown_timeout)
        self._worker: Optional["ProtectedWorkerClient"] = None
        self._sdk_initialized = False
        self._session_open = False
        self._entered = False
        self._atexit_registered = False
        self._transfer_error: Optional[BaseException] = None

    # ---------- Lifecycle ----------
    def __enter__(self) -> "CameraController":
        if self._entered:
            raise RuntimeError("Camera session is already open")

        os.makedirs(self.save_dir, exist_ok=True)
        if not os.path.isdir(self.save_dir):
            raise NotADirectoryError(
                f"Camera save directory is not a directory: {self.save_dir}"
            )

        if self.protected:
            return self._enter_protected()

        try:
            edsdk.InitializeSDK()
            self._sdk_initialized = True
            self._register_atexit()
            cam_list = edsdk.GetCameraList()
            try:
                nr_cameras = edsdk.GetChildCount(cam_list)
                if nr_cameras == 0:
                    raise RuntimeError("No cameras connected")
                if self.index >= nr_cameras:
                    raise RuntimeError(
                        f"Camera index {self.index} out of range (found {nr_cameras})"
                    )
                cam = edsdk.GetChildAtIndex(cam_list, self.index)
            finally:
                del cam_list
            self._cam = cam
            edsdk.OpenSession(cam)
            self._session_open = True

            # Event handlers (property event can be suppressed to avoid noisy warnings)
            edsdk.SetObjectEventHandler(cam, ObjectEvent.All, self._on_object_event)
            if self._register_property_events:
                try:
                    edsdk.SetPropertyEventHandler(
                        cam, PropertyEvent.All, self._on_property_event
                    )
                except Exception as e:
                    # Non-fatal: log only if verbose
                    self._log(f"Skip property events: {e}")

            # Save to host and capacity
            edsdk.SetPropertyData(cam, PropID.SaveTo, 0, int(self.save_to))
            if self.auto_capacity:
                edsdk.SetCapacity(
                    cam,
                    {
                        "reset": True,
                        "bytesPerSector": 512,
                        "numberOfFreeClusters": 2_147_483_647,
                    },
                )
            self._entered = True
            self._log("Camera session opened")
            return self
        except BaseException:
            self._close_after_failure()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception as cleanup_exc:
            if exc_type is None:
                raise
            self._log(
                f"Camera cleanup failed while handling {exc_type.__name__}: {cleanup_exc}"
            )

    def _enter_protected(self) -> "CameraController":
        if os.name != "nt":
            raise RuntimeError("protected=True is currently supported only on Windows")
        from edsdk._camera_ipc import ProtectedWorkerClient

        config = {
            "index": self.index,
            "save_dir": os.path.abspath(self.save_dir),
            "save_to": int(self.save_to),
            "auto_capacity": self.auto_capacity,
            "verbose": self.verbose,
            "register_property_events": self._register_property_events,
            "file_pattern": self._file_pattern,
            "seq_start": self._seq,
        }
        worker = ProtectedWorkerClient(
            config=config,
            event_handler=self._receive_worker_event,
            log_handler=self._log,
            start_timeout=self.worker_start_timeout,
            shutdown_timeout=self.worker_shutdown_timeout,
        )
        try:
            worker.start()
        except BaseException:
            worker.abandon()
            raise
        self._worker = worker
        self._entered = True
        self._register_atexit()
        self._log("Protected camera worker opened")
        return self

    def close(self) -> None:
        """Close the camera session and SDK, safely and idempotently."""
        if self.protected:
            self._close_protected()
            return

        errors: List[Tuple[str, BaseException]] = []
        cam = self._cam

        if self._live_view_on and cam is not None:
            try:
                self._stop_live_view_direct(suppress_errors=False)
            except BaseException as exc:
                errors.append(("stop_live_view", exc))

        if self._session_open and cam is not None:
            try:
                edsdk.CloseSession(cam)
            except BaseException as exc:
                errors.append(("close_session", exc))
            finally:
                self._session_open = False

        # Drop the last camera reference before terminating the SDK so that
        # PyEdsObject_dealloc calls EdsRelease while the SDK is still active.
        self._cam = None
        cam = None

        if self._sdk_initialized:
            try:
                edsdk.TerminateSDK()
            except BaseException as exc:
                errors.append(("terminate_sdk", exc))
            else:
                self._sdk_initialized = False

        self._live_view_on = False
        self._entered = False
        if not self._sdk_initialized:
            self._unregister_atexit()
        if not errors:
            self._log("Camera session closed")
        else:
            raise CameraCleanupError(errors)

    def _close_protected(self) -> None:
        worker = self._worker
        self._worker = None
        self._entered = False
        self._live_view_on = False
        self._unregister_atexit()
        if worker is None:
            return
        try:
            worker.close()
            self._log("Protected camera worker closed")
        except BaseException:
            worker.abandon()
            raise

    def _close_after_failure(self) -> None:
        try:
            self.close()
        except BaseException as cleanup_exc:
            self._log(f"Camera cleanup after open failure also failed: {cleanup_exc}")

    def _register_atexit(self) -> None:
        if not self._atexit_registered:
            atexit.register(self._close_at_exit)
            self._atexit_registered = True

    def _unregister_atexit(self) -> None:
        if self._atexit_registered:
            atexit.unregister(self._close_at_exit)
            self._atexit_registered = False

    def _close_at_exit(self) -> None:
        try:
            self.close()
        except BaseException as exc:
            self._log(f"Camera cleanup during interpreter shutdown failed: {exc}")

    # ---------- Event handlers ----------
    def on_object(self, fn: ObjectCallback) -> None:
        if self.protected:
            raise NotImplementedError(
                "on_object() cannot transfer EdsObject across processes; use on_event()"
            )
        self._obj_cb = fn

    def on_property(self, fn: PropertyCallback) -> None:
        if self.protected:
            raise NotImplementedError(
                "on_property() is unavailable in protected mode; use on_event()"
            )
        self._prop_cb = fn

    def on_event(self, fn: EventCallback) -> None:
        """Register a serializable event callback.

        In protected mode the callback runs on the parent-side IPC receiver thread.
        """
        if not callable(fn):
            raise TypeError("event callback must be callable")
        self._event_cb = fn

    def _on_object_event(self, event: ObjectEvent, object_handle: EdsObject) -> int:
        if event == ObjectEvent.DirItemRequestTransfer:
            # compute custom filename if pattern is provided
            dst_name: Optional[str] = None
            # 1) Highest priority: explicitly specified next filename via capture(filename=...)
            try:
                info = edsdk.GetDirectoryItemInfo(object_handle)
                orig_name = info.get("szFileName") or f"{uuid.uuid4()}.bin"
            except Exception:
                info = {}
                orig_name = f"{uuid.uuid4()}.bin"
            if self._next_filename:
                # preserve original extension; ignore any extension in provided name
                provided = self._next_filename.replace("\\", "_").replace("/", "_")
                self._next_filename = None
                base_prov, _ext_prov = os.path.splitext(provided)
                if not base_prov:
                    base_prov = "image"
                _base_orig, ext_orig = os.path.splitext(orig_name)
                if not ext_orig:
                    ext_orig = ".bin"
                dst_name = f"{base_prov}{ext_orig}"
            # 2) Next: pattern-based naming if provided
            elif self._file_pattern:
                try:
                    base, ext = os.path.splitext(orig_name)
                    if not ext:
                        ext = ".bin"
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    dst_name = self._file_pattern.format(
                        basename=base,
                        ext=ext.lstrip("."),
                        timestamp=ts,
                        seq=self._seq,
                    )
                    self._seq += 1
                except Exception:
                    dst_name = None

            try:
                path = _save_directory_item(
                    object_handle, self.save_dir, dst_basename=dst_name
                )
            except BaseException as exc:
                self._transfer_error = exc
                self._log(f"Image transfer failed: {exc}")
                code = getattr(exc, "code", None)
                return int(code) if isinstance(code, int) and code != 0 else 1
            self._saved_paths.append(path)
            self._enqueue_async_event(
                {
                    "kind": "object",
                    "event": getattr(ObjectEvent, "DirItemRequestTransfer").name,
                    "path": path,
                }
            )
        else:
            self._enqueue_async_event(
                {
                    "kind": "object",
                    "event": (
                        getattr(ObjectEvent, event.name).name
                        if hasattr(event, "name")
                        else int(event)
                    ),
                }
            )
        if self._obj_cb:
            try:
                return int(self._obj_cb(event, object_handle))
            except Exception:
                return 0
        return 0

    def _on_property_event(
        self, event: PropertyEvent, prop_id: PropID, param: int
    ) -> int:
        # Queue the serializable event even when a legacy callback is registered.
        try:
            self._enqueue_async_event(
                {
                    "kind": "property",
                    "event": event.name if hasattr(event, "name") else int(event),
                    "property": (
                        prop_id.name if hasattr(prop_id, "name") else int(prop_id)
                    ),
                    "param": int(param),
                }
            )
        except Exception:
            pass
        if self._prop_cb:
            try:
                return int(self._prop_cb(event, prop_id, param))
            except Exception:
                return 0
        return 0

    # ---------- Properties ----------
    def set_properties(
        self,
        *,
        av: Optional[Union[str, float, int]] = None,
        tv: Optional[Union[str, float, int]] = None,
        iso: Optional[Union[str, int]] = None,
        ae_mode: Optional[Union[str, int]] = None,
        metering: Optional[Union[str, int]] = None,
        white_balance: Optional[Union[str, int]] = None,
        image_quality: Optional[Union[str, int]] = None,
        drive_mode: Optional[Union[str, int]] = None,
        manual_focus: Optional[bool] = None,
        af_mode: Optional[Union[str, int]] = None,
        evf_af_mode: Optional[Union[str, int]] = None,
        nearest: bool = False,
        validate: bool = True,
        tolerate_not_supported: bool = False,
    ) -> None:
        if self.protected:
            self._call_worker(
                "set_properties",
                av=av,
                tv=tv,
                iso=iso,
                ae_mode=ae_mode,
                metering=metering,
                white_balance=white_balance,
                image_quality=image_quality,
                drive_mode=drive_mode,
                manual_focus=manual_focus,
                af_mode=af_mode,
                evf_af_mode=evf_af_mode,
                nearest=nearest,
                validate=validate,
                tolerate_not_supported=tolerate_not_supported,
            )
            return
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        # Prepare desired values
        to_set: List[Tuple[PropID, int]] = []
        if av is not None:
            codes = self._get_supported_codes(PropID.Av) if validate else ()
            to_set.append((PropID.Av, resolve_av(av, codes, nearest=nearest).code))
        if tv is not None:
            codes = self._get_supported_codes(PropID.Tv) if validate else ()
            to_set.append((PropID.Tv, resolve_tv(tv, codes, nearest=nearest).code))
        if iso is not None:
            codes = self._get_supported_codes(PropID.ISOSpeed) if validate else ()
            to_set.append(
                (PropID.ISOSpeed, resolve_iso(iso, codes, nearest=nearest).code)
            )
        if ae_mode is not None:
            to_set.append((PropID.AEMode, _enum_code(AEMode, ae_mode)))
        if metering is not None:
            to_set.append((PropID.MeteringMode, _enum_code(MeteringMode, metering)))
        if white_balance is not None:
            to_set.append(
                (PropID.WhiteBalance, _enum_code(WhiteBalance, white_balance))
            )
        if image_quality is not None:
            to_set.append(
                (PropID.ImageQuality, _enum_code(ImageQuality, image_quality))
            )
        if drive_mode is not None:
            to_set.append((PropID.DriveMode, _enum_code(DriveMode, drive_mode)))
        # Manual focus convenience flag takes precedence over af_mode
        if manual_focus is True:
            to_set.append((PropID.AFMode, int(AFMode.ManualFocus)))
        elif af_mode is not None:
            to_set.append((PropID.AFMode, _enum_code(AFMode, af_mode)))
        if evf_af_mode is not None:
            to_set.append((PropID.Evf_AFMode, _enum_code(EvfAFMode, evf_af_mode)))

        # Validate against camera descriptors; optionally tolerate AF/AEMode unsupported
        if validate:
            filtered: List[Tuple[PropID, int]] = []
            for pid, code in to_set:
                if pid in (PropID.Av, PropID.Tv, PropID.ISOSpeed):
                    # Already resolved against camera-supported codes by resolve_*
                    filtered.append((pid, code))
                    continue
                supported = self._get_supported_codes(pid)
                if supported and code not in supported:
                    if tolerate_not_supported and pid in (PropID.AEMode, PropID.AFMode):
                        self._log(
                            f"Skip unsupported {pid.name} during validate: requested {code}"
                        )
                        continue  # drop this setting silently
                    raise ValueError(f"Value {code} not supported for {pid}")
                filtered.append((pid, code))
            to_set = filtered

        # Apply
        for pid, code in to_set:
            self._log(f"Set {pid.name} -> {code}")
            try:
                edsdk.SetPropertyData(self._cam, pid, 0, code)
            except Exception as e:
                # Many Canon bodies do not allow changing AEMode via SDK.
                # Optionally ignore NOT_SUPPORTED for AEMode / AFMode when tolerate flag is set.
                if tolerate_not_supported and pid in (PropID.AEMode, PropID.AFMode):
                    self._log(f"Skip unsupported {pid.name}: {e}")
                    continue
                raise

    def get_properties(self) -> Dict[str, Union[str, int]]:
        if self.protected:
            return self._call_worker("get_properties")
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        av_code = edsdk.GetPropertyData(self._cam, PropID.Av, 0)
        tv_code = edsdk.GetPropertyData(self._cam, PropID.Tv, 0)
        iso_code = edsdk.GetPropertyData(self._cam, PropID.ISOSpeed, 0)

        # additional
        def enum_name(enum_cls, code: int) -> str:
            for name, member in enum_cls.__members__.items():
                if int(member) == int(code):
                    return name
            return str(code)

        props: Dict[str, Union[str, int]] = {
            "Av": AvTable.get(av_code, str(av_code)),
            "Tv": TvTable.get(tv_code, str(tv_code)),
            "ISO": _iso_code_to_string(int(iso_code)),
            "SaveTo": str(edsdk.GetPropertyData(self._cam, PropID.SaveTo, 0)),
            "AEMode": enum_name(
                AEMode, edsdk.GetPropertyData(self._cam, PropID.AEMode, 0)
            ),
            "MeteringMode": enum_name(
                MeteringMode, edsdk.GetPropertyData(self._cam, PropID.MeteringMode, 0)
            ),
            "WhiteBalance": enum_name(
                WhiteBalance, edsdk.GetPropertyData(self._cam, PropID.WhiteBalance, 0)
            ),
            "ImageQuality": enum_name(
                ImageQuality, edsdk.GetPropertyData(self._cam, PropID.ImageQuality, 0)
            ),
            "DriveMode": enum_name(
                DriveMode, edsdk.GetPropertyData(self._cam, PropID.DriveMode, 0)
            ),
            "AFMode": enum_name(
                AFMode,
                self._safe_get_property(PropID.AFMode),
            ),
            "EvfAFMode": enum_name(
                EvfAFMode,
                self._safe_get_property(PropID.Evf_AFMode),
            ),
        }
        return props

    # ---------- Exposure values (Av / Tv / ISO) ----------
    def _require_session(self) -> EdsObject:
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        return self._cam

    def _ensure_open(self) -> None:
        if self.protected:
            if not self._entered or self._worker is None:
                raise RuntimeError("Camera session not open")
            return
        self._require_session()

    def _call_worker(self, method: str, *args, **kwargs):
        worker = self._worker
        if not self._entered or worker is None:
            raise RuntimeError("Camera session not open")
        return worker.call(method, *args, **kwargs)

    def get_av(self) -> Aperture:
        """Return the current aperture as an :class:`Aperture`."""
        if self.protected:
            return self._call_worker("get_av")
        cam = self._require_session()
        return Aperture.from_code(int(edsdk.GetPropertyData(cam, PropID.Av, 0)))

    def get_tv(self) -> ShutterSpeed:
        """Return the current shutter speed as a :class:`ShutterSpeed`."""
        if self.protected:
            return self._call_worker("get_tv")
        cam = self._require_session()
        return ShutterSpeed.from_code(int(edsdk.GetPropertyData(cam, PropID.Tv, 0)))

    def get_iso(self) -> ISOSpeed:
        """Return the current ISO as an :class:`ISOSpeed`."""
        if self.protected:
            return self._call_worker("get_iso")
        cam = self._require_session()
        return ISOSpeed.from_code(int(edsdk.GetPropertyData(cam, PropID.ISOSpeed, 0)))

    def set_av(self, value: ApertureLike, *, nearest: bool = False) -> Aperture:
        """Set the aperture and return the value the camera reports back.

        Args:
            value: f-number (5.6), string ("f/5.6", "5.6"), or Aperture.
            nearest: Snap to the nearest supported value instead of raising.
        """
        if self.protected:
            return self._call_worker("set_av", value, nearest=nearest)
        cam = self._require_session()
        resolved = resolve_av(
            value, self._get_supported_codes(PropID.Av), nearest=nearest
        )
        self._log(f"Set Av -> {resolved}")
        edsdk.SetPropertyData(cam, PropID.Av, 0, resolved.code)
        return self.get_av()

    def set_tv(self, value: ShutterSpeedLike, *, nearest: bool = False) -> ShutterSpeed:
        """Set the shutter speed and return the value the camera reports back.

        Args:
            value: seconds (0.008), string ("1/125", "0.5s", "bulb"), or ShutterSpeed.
            nearest: Snap to the nearest supported value instead of raising.
        """
        if self.protected:
            return self._call_worker("set_tv", value, nearest=nearest)
        cam = self._require_session()
        resolved = resolve_tv(
            value, self._get_supported_codes(PropID.Tv), nearest=nearest
        )
        self._log(f"Set Tv -> {resolved}")
        edsdk.SetPropertyData(cam, PropID.Tv, 0, resolved.code)
        return self.get_tv()

    def set_iso(self, value: ISOSpeedLike, *, nearest: bool = False) -> ISOSpeed:
        """Set the ISO and return the value the camera reports back.

        Args:
            value: ISO value (400, 0=Auto), string ("400", "auto"), or ISOSpeed.
            nearest: Snap to the nearest supported value instead of raising.
        """
        if self.protected:
            return self._call_worker("set_iso", value, nearest=nearest)
        cam = self._require_session()
        resolved = resolve_iso(
            value, self._get_supported_codes(PropID.ISOSpeed), nearest=nearest
        )
        self._log(f"Set ISO -> {resolved}")
        edsdk.SetPropertyData(cam, PropID.ISOSpeed, 0, resolved.code)
        return self.get_iso()

    def supported_av(self) -> List[Aperture]:
        """Apertures supported by the connected camera/lens."""
        if self.protected:
            return self._call_worker("supported_av")
        self._require_session()
        return self._supported_entries(PropID.Av, Aperture.from_code)

    def supported_tv(self) -> List[ShutterSpeed]:
        """Shutter speeds supported by the connected camera."""
        if self.protected:
            return self._call_worker("supported_tv")
        self._require_session()
        return self._supported_entries(PropID.Tv, ShutterSpeed.from_code)

    def supported_iso(self) -> List[ISOSpeed]:
        """ISO speeds supported by the connected camera."""
        if self.protected:
            return self._call_worker("supported_iso")
        self._require_session()
        return self._supported_entries(PropID.ISOSpeed, ISOSpeed.from_code)

    def _supported_entries(
        self, pid: PropID, from_code: Callable[[int], object]
    ) -> List:
        entries: List = []
        for code in self._get_supported_codes(pid):
            try:
                entries.append(from_code(code))
            except ValueError:
                self._log(f"Skip unknown {pid.name} code 0x{code:X}")
        return entries

    # ---------- Profiles ----------
    def save_profile(self, path: str) -> None:
        """Save current properties to a JSON file."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        profile = self.get_properties()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        self._log(f"Profile saved: {path}")

    def load_profile(
        self, path: str, *, apply: bool = True, validate: bool = True
    ) -> Dict[str, Union[str, int]]:
        """Load properties from JSON file and optionally apply to the camera."""
        with open(path, "r", encoding="utf-8") as f:
            profile = json.load(f)
        if apply:
            self.set_properties(
                av=profile.get("Av"),
                tv=profile.get("Tv"),
                iso=profile.get("ISO"),
                ae_mode=profile.get("AEMode"),
                metering=profile.get("MeteringMode"),
                white_balance=profile.get("WhiteBalance"),
                image_quality=profile.get("ImageQuality"),
                drive_mode=profile.get("DriveMode"),
                af_mode=profile.get("AFMode"),
                evf_af_mode=profile.get("EvfAFMode"),
                manual_focus=True if profile.get("AFMode") == "ManualFocus" else None,
                validate=validate,
            )
        self._log(f"Profile loaded: {path}")
        return profile

    # ---------- Supported candidates ----------
    def list_supported(self) -> Dict[str, List[str]]:
        if self.protected:
            return self._call_worker("list_supported")
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        return {
            "Av": [
                AvTable.get(c, str(c)) for c in self._get_supported_codes(PropID.Av)
            ],
            "Tv": [
                TvTable.get(c, str(c)) for c in self._get_supported_codes(PropID.Tv)
            ],
            "ISO": [
                _iso_code_to_string(int(c))
                for c in self._get_supported_codes(PropID.ISOSpeed)
            ],
            "AEMode": _enum_supported_names(
                PropID.AEMode, AEMode, self._get_supported_codes(PropID.AEMode)
            ),
            "MeteringMode": _enum_supported_names(
                PropID.MeteringMode,
                MeteringMode,
                self._get_supported_codes(PropID.MeteringMode),
            ),
            "WhiteBalance": _enum_supported_names(
                PropID.WhiteBalance,
                WhiteBalance,
                self._get_supported_codes(PropID.WhiteBalance),
            ),
            "ImageQuality": _enum_supported_names(
                PropID.ImageQuality,
                ImageQuality,
                self._get_supported_codes(PropID.ImageQuality),
            ),
            "DriveMode": _enum_supported_names(
                PropID.DriveMode, DriveMode, self._get_supported_codes(PropID.DriveMode)
            ),
            "AFMode": _enum_supported_names(
                PropID.AFMode, AFMode, self._get_supported_codes(PropID.AFMode)
            ),
            "EvfAFMode": _enum_supported_names(
                PropID.Evf_AFMode,
                EvfAFMode,
                self._get_supported_codes(PropID.Evf_AFMode),
            ),
        }

    def _get_supported_codes(self, pid: PropID) -> List[int]:
        try:
            desc = edsdk.GetPropertyDesc(self._require_session(), pid)
            return list(desc.get("propDesc", ()))
        except Exception:
            return []

    # ---------- ImageQuality helpers ----------
    def get_image_quality_code(self) -> int:
        """Return current ImageQuality property code."""
        if self.protected:
            return int(self._call_worker("get_image_quality_code"))
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        return int(edsdk.GetPropertyData(self._cam, PropID.ImageQuality, 0))

    def includes_raw(self) -> bool:
        """Check if current ImageQuality setting includes RAW capture."""
        return _image_quality_includes_raw(self.get_image_quality_code())

    def is_raw_only(self) -> bool:
        """Check if current ImageQuality setting is RAW-only (no JPEG/HEIF)."""
        return _image_quality_is_raw_only(self.get_image_quality_code())

    # ---------- Capture ----------
    def capture(
        self,
        shots: int = 1,
        timeout: float = 5.0,
        *,
        interval: float = 0.0,
        retry: int = 0,
        retry_delay: float = 0.3,
        retry_on_timeout: bool = False,
        filename: Optional[str] = None,
    ) -> List[str]:
        """Take one or more photos and return every transferred host path.

        A RAW+JPEG shutter release completes only after both files arrive.
        Retrying after an accepted shutter command can create duplicate photos,
        so ``retry > 0`` requires the explicit ``retry_on_timeout=True`` opt-in.
        """
        if self.protected:
            return self._call_worker(
                "capture",
                shots,
                timeout,
                interval=interval,
                retry=retry,
                retry_delay=retry_delay,
                retry_on_timeout=retry_on_timeout,
                filename=filename,
            )
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        if isinstance(shots, bool) or not isinstance(shots, int) or shots < 1:
            raise ValueError("shots must be an integer greater than or equal to 1")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        if not math.isfinite(interval) or interval < 0:
            raise ValueError(
                "interval must be finite and greater than or equal to zero"
            )
        if isinstance(retry, bool) or not isinstance(retry, int) or retry < 0:
            raise ValueError("retry must be an integer greater than or equal to zero")
        if not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError(
                "retry_delay must be finite and greater than or equal to zero"
            )
        if retry > 0 and not retry_on_timeout:
            raise ValueError(
                "retry > 0 can take duplicate photos after a transfer timeout; "
                "pass retry_on_timeout=True to opt in explicitly"
            )
        if not (int(self.save_to) & int(SaveTo.Host)):
            raise RuntimeError(
                "capture() requires SaveTo.Host or SaveTo.Both so transfer events "
                "can be received"
            )
        self._saved_paths.clear()
        self._transfer_error = None
        if filename is not None:
            if shots != 1:
                raise ValueError("filename can be used only when shots=1")
            # Store provided name for next object transfer (extension will be preserved from camera)
            self._next_filename = filename

        expected_files = _expected_files_per_shot(self.get_image_quality_code())
        for i in range(shots):
            attempt = 0
            while True:
                start_count = len(self._saved_paths)
                try:
                    self._log(f"Trigger shot {i + 1}/{shots}")
                    edsdk.SendCommand(self._cam, CameraCommand.TakePicture, 0)
                    self._wait_for_transfer(
                        timeout,
                        start_count=start_count,
                        expected_count=expected_files,
                    )
                    break
                except TimeoutError:
                    if self._transfer_error is not None:
                        raise
                    received = len(self._saved_paths) - start_count
                    if received:
                        raise
                    if attempt >= retry:
                        raise
                    attempt += 1
                    self._log(
                        f"Wait before retrying shot {i + 1}/{shots} "
                        f"(attempt {attempt}/{retry})"
                    )
                    try:
                        self._wait_for_transfer(
                            retry_delay,
                            start_count=start_count,
                            expected_count=expected_files,
                        )
                    except TimeoutError:
                        if self._transfer_error is not None:
                            raise
                        received = len(self._saved_paths) - start_count
                        if received:
                            raise
                        continue
                    else:
                        break
            if interval > 0 and i < shots - 1:
                time.sleep(interval)
        return list(self._saved_paths)

    def _wait_for_transfer(
        self, timeout: float, *, start_count: int, expected_count: int
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.01)
            _pump_messages_once()
            if self._transfer_error is not None:
                raise self._transfer_error
            received = len(self._saved_paths) - start_count
            if received >= expected_count:
                return
        if self._transfer_error is not None:
            raise self._transfer_error
        received = max(0, len(self._saved_paths) - start_count)
        raise TimeoutError(
            "Timed out waiting for image transfer events "
            f"(expected {expected_count}, received {received})"
        )

    # ---------- Capture to memory ----------
    def capture_bytes(
        self,
        shots: int = 1,
        timeout: float = 5.0,
        *,
        interval: float = 0.0,
        retry: int = 0,
        retry_delay: float = 0.3,
        retry_on_timeout: bool = False,
        keep_files: bool = False,
    ) -> List[bytes]:
        """Capture and return image bytes in memory.

        ``retry_on_timeout`` has the same duplicate-photo warning as
        :meth:`capture`.

        Optionally keeps or removes the saved files from disk (default: remove).
        """
        paths = self.capture(
            shots=shots,
            timeout=timeout,
            interval=interval,
            retry=retry,
            retry_delay=retry_delay,
            retry_on_timeout=retry_on_timeout,
        )
        data_list: List[bytes] = []
        for p in paths:
            try:
                with open(p, "rb") as f:
                    data_list.append(f.read())
            finally:
                if not keep_files:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        return data_list

    def capture_numpy(
        self,
        shots: int = 1,
        timeout: float = 5.0,
        *,
        interval: float = 0.0,
        retry: int = 0,
        retry_delay: float = 0.3,
        retry_on_timeout: bool = False,
        keep_files: bool = False,
        raw_processor: Optional[RawProcessor] = None,
    ) -> List["np.ndarray"]:
        """Capture and return a list of numpy arrays (RGB).

        こちらが設計の軸になるメソッドです。

        Args:
            shots: Number of shots to capture.
            timeout: Timeout in seconds for each shot transfer.
            interval: Interval in seconds between shots.
            retry: Number of retries on timeout.
            retry_delay: Delay in seconds between retries.
            retry_on_timeout: Explicitly allow another shutter command after
                              a transfer timeout. This can create duplicate photos.
            keep_files: If True, keep captured files on disk.
            raw_processor: Callback to develop RAW images.
                           Must conform to RawProcessor protocol:
                           (raw_bytes: bytes) -> numpy.ndarray
                           Required if camera is set to RAW or RAW+JPEG mode.
                           See RawProcessor docstring for examples.

        Returns:
            List of numpy arrays (RGB format, dtype 任意).

        Raises:
            ValueError: If RAW capture is enabled but no raw_processor provided.
            TypeError: If raw_processor has invalid signature or returns non-ndarray.
            RuntimeError: If numpy is not installed or camera session not open.
        """
        try:
            import numpy as np  # type: ignore
        except Exception as e:
            raise RuntimeError("numpy is required for capture_numpy()") from e

        # JPEG/HEIF デコード用の代替ライブラリ（imageio.v3）を準備
        try:
            import imageio.v3 as iio  # type: ignore
        except Exception as e:
            # RAW のみを raw_processor で処理するモードなら許容し、
            # JPEG/HEIF を含む場合に備えて明示的なエラーメッセージを出す
            iio = None  # type: ignore[assignment]
            imageio_import_error: Optional[Exception] = e
        else:
            imageio_import_error = None

        # Check current ImageQuality setting before capture
        self._ensure_open()

        quality_code = self.get_image_quality_code()
        includes_raw = _image_quality_includes_raw(quality_code)

        # Validate raw_processor if provided
        if raw_processor is not None:
            _validate_raw_processor(raw_processor)

        if includes_raw and raw_processor is None:
            # Get human-readable name for error message
            quality_name = "Unknown"
            for name, member in ImageQuality.__members__.items():
                if int(member) == quality_code:
                    quality_name = name
                    break
            raise ValueError(
                f"Camera ImageQuality is set to '{quality_name}' which includes RAW, "
                "but no raw_processor was provided.\n"
                "Either pass a raw_processor callback or change camera to JPEG-only mode.\n"
                "See RawProcessor docstring for implementation examples."
            )

        # Capture files
        paths = self.capture(
            shots=shots,
            timeout=timeout,
            interval=interval,
            retry=retry,
            retry_delay=retry_delay,
            retry_on_timeout=retry_on_timeout,
        )

        arrays: List["np.ndarray"] = []
        for p in paths:
            try:
                with open(p, "rb") as f:
                    raw_bytes = f.read()

                if _is_raw_file(p):
                    # RAW file: use processor
                    if raw_processor is not None:
                        self._log(
                            f"Processing RAW file with raw_processor: {os.path.basename(p)}"
                        )
                        arr = raw_processor(raw_bytes)
                        # Validate return type
                        if not isinstance(arr, np.ndarray):
                            raise TypeError(
                                f"raw_processor must return numpy.ndarray, "
                                f"but got {type(arr).__name__}.\n"
                                "See RawProcessor docstring for correct implementation."
                            )
                    else:
                        # This shouldn't happen if we checked above, but safety fallback
                        raise RuntimeError(
                            f"RAW file detected ({os.path.basename(p)}), "
                            "but no raw_processor provided."
                        )
                else:
                    # JPEG/HEIF: decode via imageio instead of Pillow
                    if iio is None:
                        raise RuntimeError(
                            "imageio (imageio.v3) is required to decode JPEG/HEIF "
                            "for capture_numpy(). Install via 'pip install imageio'."
                        ) from imageio_import_error
                    self._log(
                        f"Loading image with imageio -> numpy: {os.path.basename(p)}"
                    )
                    # imageio.v3.imread は bytes も扱える
                    arr = iio.imread(raw_bytes)

                arrays.append(arr)
            except Exception as e:
                if _is_raw_file(p) and raw_processor is None:
                    raise RuntimeError(
                        f"Failed to process {os.path.basename(p)}. "
                        "If this is a RAW file, provide a raw_processor callback."
                    ) from e
                raise
            finally:
                if not keep_files:
                    try:
                        os.remove(p)
                    except Exception:
                        pass

        return arrays

    # ---------- Live View ----------
    def start_live_view(self) -> None:
        if self.protected:
            self._call_worker("start_live_view")
            self._live_view_on = True
            return
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        # Enable LV to PC
        edsdk.SetPropertyData(self._cam, PropID.Evf_Mode, 0, int(1))
        edsdk.SetPropertyData(
            self._cam, PropID.Evf_OutputDevice, 0, int(EvfOutputDevice.PC)
        )
        self._live_view_on = True
        self._log("Live view started")

    def stop_live_view(self) -> None:
        if self.protected:
            if self._worker is not None and self._entered:
                self._call_worker("stop_live_view")
            self._live_view_on = False
            return
        if self._cam is None:
            return
        self._stop_live_view_direct(suppress_errors=True)

    def _stop_live_view_direct(self, *, suppress_errors: bool) -> None:
        if self._cam is None:
            self._live_view_on = False
            return
        errors: List[Tuple[str, BaseException]] = []
        try:
            edsdk.SetPropertyData(
                self._cam, PropID.Evf_OutputDevice, 0, int(EvfOutputDevice.TFT)
            )
        except BaseException as exc:
            errors.append(("evf_output_device", exc))
        try:
            edsdk.SetPropertyData(self._cam, PropID.Evf_Mode, 0, int(0))
        except BaseException as exc:
            errors.append(("evf_mode", exc))
        self._live_view_on = False
        self._log("Live view stopped")
        if errors and not suppress_errors:
            raise CameraCleanupError(errors)

    def grab_live_view_frame(self, save_path: Optional[str] = None) -> LiveViewData:
        if self.protected:
            result = self._call_worker("grab_live_view_frame", save_path=save_path)
            self._live_view_on = True
            return result
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        if not self._live_view_on:
            self.start_live_view()
            # give camera a brief moment to deliver first frame
            time.sleep(0.1)
        # Retry loop for transient OBJECT_NOTREADY / DEVICE_BUSY conditions
        MAX_ATTEMPTS = 10
        RETRY_DELAY = 0.07  # ~70ms between attempts
        ERR_OBJECT_NOT_READY = 0x0000A102
        ERR_DEVICE_BUSY = 0x00000081
        last_exc: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if save_path is not None:
                    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                    out_stream = edsdk.CreateFileStream(
                        save_path, FileCreateDisposition.CreateAlways, Access.ReadWrite
                    )
                    evf_image = edsdk.CreateEvfImageRef(out_stream)
                    edsdk.DownloadEvfImage(self._cam, evf_image)
                    self._log(
                        f"Live view saved: {save_path} (attempt {attempt}/{MAX_ATTEMPTS})"
                    )
                    return save_path
                # Default: download EVF frame into a pre-allocated in-memory buffer.
                # This avoids creating any temporary files on disk.
                buf_size = 8 * 1024 * 1024  # 8 MiB initial buffer
                max_buf_size = 64 * 1024 * 1024  # 64 MiB cap
                last_buf_exc: Optional[Exception] = None

                while buf_size <= max_buf_size:
                    try:
                        buf = bytearray(buf_size)
                        out_stream = edsdk.CreateMemoryStreamFromPointer(buf)
                        evf_image = edsdk.CreateEvfImageRef(out_stream)
                        edsdk.DownloadEvfImage(self._cam, evf_image)

                        # Prefer position (bytes written). Fallback to length.
                        n = int(edsdk.GetPosition(out_stream))
                        if n <= 0:
                            n = int(edsdk.GetLength(out_stream))
                        if n <= 0:
                            raise RuntimeError(
                                "Live view download returned empty buffer"
                            )
                        if n > len(buf):
                            raise RuntimeError(
                                f"Live view wrote {n} bytes into a {len(buf)} byte buffer"
                            )

                        data = bytes(memoryview(buf)[:n])
                        self._log(
                            f"Live view grabbed: {len(data)} bytes (attempt {attempt}/{MAX_ATTEMPTS})"
                        )
                        return data
                    except Exception as e:
                        # If buffer is too small, retry with a larger one.
                        last_buf_exc = e
                        msg = str(e)
                        code = getattr(e, "code", None)
                        might_be_too_small = (
                            (code == 0x00000065)  # EDS_ERR_MEM_ALLOC_FAILED (common)
                            or ("BUFFER" in msg.upper())
                            or ("MEM" in msg.upper() and "ALLOC" in msg.upper())
                        )
                        if not might_be_too_small:
                            raise
                        buf_size *= 2
                        continue

                if last_buf_exc is not None:
                    raise last_buf_exc
                raise RuntimeError("Live view download failed without exception")
            except Exception as e:  # Catch SDK error
                code = getattr(e, "code", None)
                msg = str(e)
                # Detect transient errors
                is_transient = False
                if code in (ERR_OBJECT_NOT_READY, ERR_DEVICE_BUSY):
                    is_transient = True
                elif "OBJECT_NOTREADY" in msg or "DEVICE_BUSY" in msg:
                    is_transient = True
                if not is_transient or attempt >= MAX_ATTEMPTS:
                    last_exc = e
                    break
                # Backoff and allow Windows message pump to progress
                self._log(
                    f"Live view retry {attempt}/{MAX_ATTEMPTS} after transient error: {msg}"
                )
                _pump_messages_once()
                time.sleep(RETRY_DELAY)
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unexpected live view failure without exception")

    def grab_live_view_numpy(self) -> "np.ndarray":
        """Grab one live-view frame and return as numpy array (requires numpy and imageio)."""
        try:
            import numpy as np  # type: ignore
        except Exception as e:
            raise RuntimeError("numpy is required for grab_live_view_numpy()") from e

        try:
            import imageio.v3 as iio  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "imageio (imageio.v3) is required for grab_live_view_numpy(). "
                "Install via 'pip install imageio'."
            ) from e

        data = self.grab_live_view_frame()
        if isinstance(data, str):
            # ファイルパスが返ってきた場合はファイルから読む
            with open(data, "rb") as f:
                raw = f.read()
        else:
            raw = data

        # imageio で bytes から直接 numpy 配列へ
        arr = iio.imread(raw)
        return arr

    # ---------- asyncio event queue ----------
    def enable_async(
        self, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> asyncio.Queue[CameraEvent]:
        """Enable async event queue; returns asyncio.Queue for events."""
        if loop is None:
            loop = asyncio.get_event_loop()
        self._async_loop = loop
        self._async_queue = asyncio.Queue()
        self._log("Async event queue enabled")
        return self._async_queue

    def disable_async(self) -> None:
        self._async_queue = None
        self._async_loop = None
        self._async_pumping = False
        self._log("Async event queue disabled")

    async def pump_events(self, interval: float = 0.01) -> None:
        """Run message pumping periodically in asyncio task (Windows required)."""
        self._async_pumping = True
        try:
            while self._async_pumping:
                _pump_messages_once()
                await asyncio.sleep(interval)
        finally:
            self._async_pumping = False

    def _receive_worker_event(self, evt: CameraEvent) -> None:
        self._enqueue_async_event(evt)

    def _enqueue_async_event(self, evt: CameraEvent) -> None:
        if self._event_cb is not None:
            try:
                self._event_cb(evt)
            except Exception as exc:
                self._log(f"Camera event callback failed: {exc}")
        if self._async_queue is None or self._async_loop is None:
            return
        try:
            self._async_loop.call_soon_threadsafe(self._async_queue.put_nowait, evt)
        except Exception:
            pass

    # ---------- Helpers ----------
    def _safe_get_property(self, pid: PropID) -> int:
        """Return property value or -1 if unsupported (to avoid raising)."""
        try:
            return int(edsdk.GetPropertyData(self._cam, pid, 0))  # type: ignore[arg-type]
        except Exception:
            return -1


def _iso_code_to_string(code: int) -> str:
    try:
        if code == int(ISOSpeedCamera.ISOAuto):
            return "Auto"
        for name in ISOSpeedCamera.__members__:
            if int(getattr(ISOSpeedCamera, name)) == code:
                return name.replace("ISO", "")
    except Exception:
        pass
    return str(code)


def classify_error(exc: Exception) -> Dict[str, Union[int, str, None]]:
    """Return a structured error info for EdsError exceptions.
    Includes SDK error code and human-readable message from edsdk_utils.
    """
    try:
        if isinstance(exc, getattr(edsdk, "EdsError", Exception)):
            code = getattr(exc, "code", None)
            return {
                "code": int(code) if code is not None else None,
                "message": str(exc),
            }
    except Exception:
        pass
    return {"message": str(exc)}


def _enum_code(enum_cls: Type[IntEnum], value: Union[str, int]) -> int:
    if isinstance(value, int):
        return int(value)
    key = str(value).strip()
    # Friendly aliases for some enums
    alias_key = key.lower().replace(" ", "").replace("-", "").replace("_", "")
    try:
        enum_name = enum_cls.__name__
    except Exception:
        enum_name = ""
    # MeteringMode aliases
    if enum_name == "MeteringMode":
        aliases = {
            "evaluative": "EvaluativeMetering",
            "spot": "PartialMetering",
            "partial": "PartialMetering",
            "centerweighted": "CenterWeightedAveragingMetering",
            "centerweightedaverage": "CenterWeightedAveragingMetering",
            "average": "CenterWeightedAveragingMetering",
        }
        if alias_key in aliases:
            key = aliases[alias_key]
    # Accept case-insensitive and some friendly aliases
    for name, member in enum_cls.__members__.items():
        if name.lower() == key.lower():
            return int(member)
    # Also accept numeric string
    if key.isdigit():
        return int(key)
    raise ValueError(f"Unsupported value '{value}' for {enum_cls.__name__}")


def _enum_supported_names(
    pid: _PropIDEnum, enum_cls: Type[IntEnum], codes: List[int]
) -> List[str]:
    names: List[str] = []
    if not codes:
        # if descriptors not available, return all enum names as hint
        return list(enum_cls.__members__.keys())
    for code in codes:
        matched = False
        for name, member in enum_cls.__members__.items():
            if int(member) == int(code):
                names.append(name)
                matched = True
                break
        if not matched:
            names.append(str(code))
    return names
