"""Hardware integration tests for exposure APIs.

Requires a Canon camera connected via USB, powered on, mode dial at M
(Manual). Auto-skips when no camera is available.

Run: .venv\\Scripts\\python.exe -m pytest tests/test_exposure_hardware.py -v
"""

import pytest

pytest.importorskip("edsdk")

from edsdk.camera_controller import CameraController
from edsdk.exposure import Aperture, ISOSpeed, ShutterSpeed


@pytest.fixture(scope="module")
def cam():
    try:
        controller = CameraController().__enter__()
    except Exception as exc:
        pytest.skip(f"No camera available: {exc}")
    try:
        props = controller.get_properties()
        if props.get("AEMode") != "Manual":
            pytest.skip(
                f"Camera must be in Manual mode, currently: {props.get('AEMode')}"
            )
        original = (controller.get_av(), controller.get_tv(), controller.get_iso())
        try:
            yield controller
        finally:
            controller.set_av(original[0])
            controller.set_tv(original[1])
            controller.set_iso(original[2])
    finally:
        controller.__exit__(None, None, None)


def test_supported_values(cam):
    apertures = cam.supported_av()
    speeds = cam.supported_tv()
    isos = cam.supported_iso()
    assert apertures and all(isinstance(a, Aperture) for a in apertures)
    assert speeds and all(isinstance(t, ShutterSpeed) for t in speeds)
    assert isos and all(isinstance(i, ISOSpeed) for i in isos)


def test_get_current_values(cam):
    assert isinstance(cam.get_av(), Aperture)
    assert isinstance(cam.get_tv(), ShutterSpeed)
    assert isinstance(cam.get_iso(), ISOSpeed)


def test_set_av_roundtrip(cam):
    supported = cam.supported_av()
    target = supported[len(supported) // 2]
    result = cam.set_av(target.f_number)
    assert result.code == target.code


def test_set_tv_roundtrip(cam):
    numeric = [t for t in cam.supported_tv() if t.seconds is not None]
    target = numeric[len(numeric) // 2]
    result = cam.set_tv(target.display)
    assert result.code == target.code


def test_set_iso_roundtrip(cam):
    numeric = [i for i in cam.supported_iso() if i.value is not None]
    target = numeric[len(numeric) // 2]
    result = cam.set_iso(target.value)
    assert result.code == target.code


def test_strict_rejects_out_of_range_av(cam):
    widest = min(cam.supported_av(), key=lambda a: a.f_number)
    too_wide = widest.f_number / 1.4  # レンズ開放より1段明るい値
    with pytest.raises(ValueError) as excinfo:
        cam.set_av(too_wide)
    message = str(excinfo.value)
    assert "nearest=True" in message


def test_nearest_snaps_av(cam):
    widest = min(cam.supported_av(), key=lambda a: a.f_number)
    result = cam.set_av(widest.f_number / 1.4, nearest=True)
    assert result.code == widest.code


def test_set_properties_densoten_style(cam):
    """Densoten リポジトリと同じ Config 文字列渡しの呼び出し形。"""
    apertures = cam.supported_av()
    speeds = [t for t in cam.supported_tv() if t.seconds is not None]
    isos = [i for i in cam.supported_iso() if i.value is not None]
    cam.set_properties(
        av=f"{apertures[0].f_number:g}",
        tv=speeds[0].display,
        iso=str(isos[0].value),
    )
    assert cam.get_av().f_number == pytest.approx(apertures[0].f_number)


def test_densoten_default_config_values(cam):
    """Densoten configs/default.toml のリテラル値(対応カメラのみ)。"""
    try:
        cam.set_properties(av="5", tv="1/30", iso="500")
    except ValueError as exc:
        pytest.skip(f"Camera does not support Densoten defaults: {exc}")
    assert cam.get_av().f_number == pytest.approx(5.0)
    assert cam.get_tv().seconds == pytest.approx(1 / 30)
    assert cam.get_iso().value == 500
