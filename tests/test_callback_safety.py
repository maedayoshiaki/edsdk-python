"""Regression tests for callback GIL and context ownership."""

from __future__ import annotations

import gc
import sys
import weakref
from pathlib import Path

import pytest

from edsdk import api
from edsdk.constants import (
    Access,
    FileCreateDisposition,
    ProgressOption,
)


@pytest.fixture
def initialized_sdk():
    api.InitializeSDK()
    try:
        yield
    finally:
        gc.collect()
        api.TerminateSDK()


def _open_source_stream():
    source_path = Path(__file__).parents[1] / "README.md"
    return api.CreateFileStream(
        str(source_path),
        FileCreateDisposition.OpenExisting,
        Access.Read,
    )


def test_progress_callbacks_have_isolated_contexts(initialized_sdk):
    calls = []
    first_context = object()
    second_context = object()

    def first_callback(percent, cancel, context):
        calls.append(("first", percent, cancel, context))
        return 0

    def second_callback(percent, cancel, context):
        calls.append(("second", percent, cancel, context))
        return 0

    first_source = _open_source_stream()
    second_source = _open_source_stream()
    first_output = api.CreateMemoryStream(1_000_000)
    second_output = api.CreateMemoryStream(1_000_000)

    api.SetProgressCallback(
        first_output,
        first_callback,
        ProgressOption.Done,
        first_context,
    )
    api.SetProgressCallback(
        second_output,
        second_callback,
        ProgressOption.Done,
        second_context,
    )
    api.CopyData(
        first_source,
        api.GetLength(first_source),
        first_output,
    )
    api.CopyData(
        second_source,
        api.GetLength(second_source),
        second_output,
    )

    assert calls == [
        ("first", 100, False, first_context),
        ("second", 100, False, second_context),
    ]


def test_callback_exception_is_unraisable_and_does_not_poison_gil(
    initialized_sdk,
    monkeypatch,
):
    unraisable = []
    monkeypatch.setattr(
        sys,
        "unraisablehook",
        lambda args: unraisable.append(args),
    )

    def broken_callback(percent, cancel):
        raise RuntimeError("callback boom")

    source = _open_source_stream()
    output = api.CreateMemoryStream(1_000_000)
    api.SetProgressCallback(
        output,
        broken_callback,
        ProgressOption.Done,
    )

    with pytest.raises(api.EdsError, match="INVALID_FN_POINTER"):
        api.CopyData(source, api.GetLength(source), output)

    assert len(unraisable) == 1
    assert isinstance(unraisable[0].exc_value, RuntimeError)
    assert str(unraisable[0].exc_value) == "callback boom"

    follow_up_calls = []
    follow_up_source = _open_source_stream()
    follow_up_output = api.CreateMemoryStream(1_000_000)
    api.SetProgressCallback(
        follow_up_output,
        lambda percent, cancel: follow_up_calls.append(percent) or 0,
        ProgressOption.Done,
    )
    api.CopyData(
        follow_up_source,
        api.GetLength(follow_up_source),
        follow_up_output,
    )
    assert follow_up_calls == [100]


def test_camera_added_handler_accepts_zero_or_one_parameter():
    api.InitializeSDK()
    try:
        api.SetCameraAddedHandler(lambda: 0)
        api.SetCameraAddedHandler(lambda context: 0, object())
    finally:
        api.TerminateSDK()


def test_callback_references_are_released_on_terminate():
    class Context:
        pass

    callback = lambda context: 0
    context = Context()
    callback_ref = weakref.ref(callback)
    context_ref = weakref.ref(context)

    api.InitializeSDK()
    try:
        api.SetCameraAddedHandler(callback, context)
        del callback
        del context
        gc.collect()
        assert callback_ref() is not None
        assert context_ref() is not None
    finally:
        api.TerminateSDK()

    gc.collect()
    assert callback_ref() is None
    assert context_ref() is None
