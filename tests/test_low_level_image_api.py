"""Argument-contract tests for the low-level image APIs."""

from __future__ import annotations

import pytest

from edsdk import api
from edsdk.constants import ImageSource, TargetImageType


SOURCE_RECT = {
    "point": {"x": 0, "y": 0},
    "size": {"width": 1, "height": 1},
}
DEST_SIZE = {"width": 1, "height": 1}


def test_download_thumbnail_requires_destination_stream():
    with pytest.raises(TypeError):
        api.DownloadThumbnail(object())

    with pytest.raises(ValueError, match="invalid EdsObject"):
        api.DownloadThumbnail(object(), object())


def test_get_image_requires_destination_stream():
    with pytest.raises(TypeError):
        api.GetImage(
            object(),
            ImageSource.FullView,
            TargetImageType.RGB,
            SOURCE_RECT,
            DEST_SIZE,
        )

    with pytest.raises(ValueError, match="invalid EdsObject"):
        api.GetImage(
            object(),
            ImageSource.FullView,
            TargetImageType.RGB,
            SOURCE_RECT,
            DEST_SIZE,
            object(),
        )


@pytest.mark.parametrize(
    ("source_rect", "dest_size"),
    [
        ([], DEST_SIZE),
        (SOURCE_RECT, []),
    ],
)
def test_get_image_rejects_non_dict_geometry(source_rect, dest_size):
    with pytest.raises(TypeError):
        api.GetImage(
            object(),
            ImageSource.FullView,
            TargetImageType.RGB,
            source_rect,
            dest_size,
            object(),
        )
