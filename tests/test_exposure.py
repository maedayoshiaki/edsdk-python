"""Unit tests for edsdk.exposure (no camera required)."""

import math

import pytest

from edsdk.constants.properties import Av as AvTable
from edsdk.constants.properties import Tv as TvTable
from edsdk.exposure import (
    APERTURES,
    ISO_SPEEDS,
    SHUTTER_SPEEDS,
    Aperture,
    ISOSpeed,
    ShutterSpeed,
)


class TestTables:
    def test_apertures_cover_table(self):
        # NotValid (0xFFFFFFFF) だけが除外される
        assert len(APERTURES) == len(AvTable) - 1
        assert all(a.f_number > 0 for a in APERTURES)

    def test_aperture_values(self):
        by_code = {a.code: a for a in APERTURES}
        assert by_code[0x30].f_number == pytest.approx(5.6)
        assert by_code[0x30].display == "5.6"
        assert by_code[0x25].f_number == pytest.approx(3.5)  # "3.5 (1/3)"
        assert by_code[0x08].f_number == pytest.approx(1.0)

    def test_shutter_speeds_cover_table(self):
        assert len(SHUTTER_SPEEDS) == len(TvTable) - 1

    def test_shutter_speed_values(self):
        by_code = {t.code: t for t in SHUTTER_SPEEDS}
        assert by_code[0x0C].seconds is None
        assert by_code[0x0C].is_bulb
        assert by_code[0x10].seconds == pytest.approx(30.0)  # 30"
        assert by_code[0x2B].seconds == pytest.approx(3.2)  # 3"2
        assert by_code[0x45].seconds == pytest.approx(0.3)  # 0"3 (1/3)
        assert by_code[0x70].seconds == pytest.approx(1 / 125)  # 1/125
        assert by_code[0x4D].seconds == pytest.approx(1 / 6)  # 1/6 (1/3)
        assert by_code[0x2C].seconds == pytest.approx(3.0)  # plain "3"

    def test_iso_values(self):
        by_code = {i.code: i for i in ISO_SPEEDS}
        assert by_code[0x48].value == 100
        assert by_code[0x00].value is None
        assert by_code[0x00].is_auto
        assert by_code[0x00].display == "Auto"

    def test_from_code(self):
        assert Aperture.from_code(0x30).f_number == pytest.approx(5.6)
        assert ShutterSpeed.from_code(0x70).display == "1/125"
        assert ISOSpeed.from_code(0x48).value == 100

    def test_from_code_rejects_unknown(self):
        with pytest.raises(ValueError):
            Aperture.from_code(0xFFFFFFFF)
        with pytest.raises(ValueError):
            ShutterSpeed.from_code(0x999)
        with pytest.raises(ValueError):
            ISOSpeed.from_code(0x999)

    def test_str(self):
        assert str(Aperture.from_code(0x30)) == "f/5.6"
        assert str(ShutterSpeed.from_code(0x70)) == "1/125"
        assert str(ISOSpeed.from_code(0x48)) == "ISO 100"
