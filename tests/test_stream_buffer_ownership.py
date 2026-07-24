"""Regression tests for Python-owned buffers passed to EDSDK streams."""

from __future__ import annotations

import gc
import weakref

import pytest

from edsdk import api


@pytest.fixture
def initialized_sdk():
    api.InitializeSDK()
    try:
        yield
    finally:
        gc.collect()
        api.TerminateSDK()


def test_stream_holds_buffer_export_until_release(initialized_sdk):
    buffer = bytearray(b"camera-data")
    stream = api.CreateMemoryStreamFromPointer(buffer)

    assert api.GetLength(stream) == len(buffer)
    with pytest.raises(BufferError):
        buffer.extend(b"!")

    del stream
    gc.collect()

    buffer.extend(b"!")
    assert buffer == b"camera-data!"


def test_stream_keeps_buffer_owner_alive(initialized_sdk):
    class WeakByteArray(bytearray):
        pass

    buffer = WeakByteArray(b"camera-data")
    owner_ref = weakref.ref(buffer)
    stream = api.CreateMemoryStreamFromPointer(buffer)

    del buffer
    gc.collect()
    assert owner_ref() is not None

    del stream
    gc.collect()
    assert owner_ref() is None


def test_stream_accepts_writable_contiguous_memoryview(initialized_sdk):
    buffer = bytearray(b"camera-data")
    view = memoryview(buffer)
    stream = api.CreateMemoryStreamFromPointer(view)

    del view
    gc.collect()
    with pytest.raises(BufferError):
        buffer.extend(b"!")

    del stream
    gc.collect()
    buffer.extend(b"!")
    assert buffer == b"camera-data!"


@pytest.mark.parametrize(
    "buffer",
    [
        b"read-only",
        memoryview(b"read-only"),
        memoryview(bytearray(b"not-contiguous"))[::2],
    ],
)
def test_stream_rejects_unsafe_buffers(initialized_sdk, buffer):
    with pytest.raises((TypeError, BufferError)):
        api.CreateMemoryStreamFromPointer(buffer)


def test_failed_directory_delete_keeps_object_valid(initialized_sdk):
    stream = api.CreateMemoryStream(16)

    with pytest.raises(api.EdsError):
        api.DeleteDirectoryItem(stream)

    assert isinstance(api.GetLength(stream), int)
