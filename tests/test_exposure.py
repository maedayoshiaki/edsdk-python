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
    resolve_av,
    resolve_iso,
    resolve_tv,
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


# 実機 EOS M6 Mark II (EF-M15-45mm) の GetPropertyDesc 実測値
AV_M6 = [0x25, 0x28, 0x2B, 0x2D, 0x30, 0x33, 0x35, 0x38, 0x3B, 0x3D,
         0x40, 0x43, 0x45, 0x48, 0x4B, 0x4D, 0x50]
TV_M6 = [0x0C, 0x10, 0x13, 0x15, 0x18, 0x1B, 0x1D, 0x20, 0x23, 0x25,
         0x28, 0x2B, 0x2D, 0x30, 0x33, 0x35, 0x38, 0x3B, 0x3D, 0x40,
         0x43, 0x45, 0x48, 0x4B, 0x4D, 0x50, 0x53, 0x55, 0x58, 0x5B,
         0x5D, 0x60, 0x63, 0x65, 0x68, 0x6B, 0x6D, 0x70, 0x73, 0x75,
         0x78, 0x7B, 0x7D, 0x80, 0x83, 0x85, 0x88, 0x8B, 0x8D, 0x90,
         0x93, 0x95, 0x98]
ISO_ALL = [i.code for i in ISO_SPEEDS]


class TestFromAny:
    def test_av_numeric(self):
        assert Aperture.from_any(5.6).code == 0x30
        assert Aperture.from_any(8).code == 0x38
        assert Aperture.from_any(2.8).code == 0x20

    def test_av_numeric_rounding_tolerance(self):
        # 計算値 sqrt(2)^5 = 5.657 は公称段 f/5.6 に吸収される
        assert Aperture.from_any(math.sqrt(2) ** 5).code == 0x30

    def test_av_duplicate_value_picks_smallest_code(self):
        assert Aperture.from_any(3.5).code == 0x24  # not 0x25 "3.5 (1/3)"
        assert Aperture.from_any(4.5).code == 0x2B  # not 0x2C
        assert Aperture.from_any("4.5").code == 0x2B

    def test_av_strings(self):
        for text in ("f/5.6", "F/5.6", "5.6", "f5.6", "f 5.6", "5.6f"):
            assert Aperture.from_any(text).code == 0x30, text

    def test_av_display_exact(self):
        assert Aperture.from_any("3.5 (1/3)").code == 0x25
        assert Aperture.from_any("3.5").code == 0x24

    def test_av_invalid(self):
        with pytest.raises(ValueError):
            Aperture.from_any("banana")
        with pytest.raises(ValueError):
            Aperture.from_any(0.05)
        with pytest.raises(ValueError):
            Aperture.from_any(-1)

    def test_av_instance_passthrough(self):
        entry = Aperture.from_code(0x30)
        assert Aperture.from_any(entry) is entry

    def test_tv_numeric(self):
        assert ShutterSpeed.from_any(0.5).code == 0x40  # 0"5
        assert ShutterSpeed.from_any(1 / 125).code == 0x70
        assert ShutterSpeed.from_any(30).code == 0x10
        assert ShutterSpeed.from_any(2).code == 0x30

    def test_tv_strings(self):
        for text in ("1/125", "1/125s", "1/125sec"):
            assert ShutterSpeed.from_any(text).code == 0x70, text
        for text in ("0.5", "0.5s", '0"5'):
            assert ShutterSpeed.from_any(text).code == 0x40, text
        assert ShutterSpeed.from_any('30"').code == 0x10
        assert ShutterSpeed.from_any("1/10 (1/3)").code == 0x53
        assert ShutterSpeed.from_any("bulb").is_bulb
        assert ShutterSpeed.from_any("Bulb").is_bulb

    def test_tv_invalid(self):
        with pytest.raises(ValueError):
            ShutterSpeed.from_any("banana")
        with pytest.raises(ValueError):
            ShutterSpeed.from_any(0)
        with pytest.raises(ValueError):
            ShutterSpeed.from_any(-2)

    def test_iso(self):
        assert ISOSpeed.from_any(100).code == 0x48
        assert ISOSpeed.from_any(0).is_auto
        assert ISOSpeed.from_any("auto").is_auto
        assert ISOSpeed.from_any("Auto").is_auto
        assert ISOSpeed.from_any("ISOAuto").is_auto
        assert ISOSpeed.from_any("ISO100").code == 0x48
        assert ISOSpeed.from_any("100").code == 0x48

    def test_iso_requires_exact_value(self):
        with pytest.raises(ValueError):
            ISOSpeed.from_any(110)


class TestResolve:
    def test_supported_exact(self):
        assert resolve_av(4.0, AV_M6).code == 0x28

    def test_prefers_supported_code_among_equal_values(self):
        # F3.5 は 0x24 と 0x25 "3.5 (1/3)" の2コード。M6 II は 0x25 のみ対応
        assert resolve_av(3.5, AV_M6).code == 0x25
        assert resolve_av("3.5", AV_M6).code == 0x25
        assert resolve_av("3.5 (1/3)", AV_M6).code == 0x25

    def test_strict_raises_with_helpful_message(self):
        with pytest.raises(ValueError) as excinfo:
            resolve_av(2.8, AV_M6)
        message = str(excinfo.value)
        assert "f/2.8" in message
        assert "f/3.5" in message  # nearest 候補の提示
        assert "nearest=True" in message

    def test_nearest_av(self):
        assert resolve_av(2.8, AV_M6, nearest=True).code == 0x25

    def test_nearest_tv(self):
        assert resolve_tv(1 / 8000, TV_M6, nearest=True).code == 0x98  # -> 1/4000

    def test_nearest_iso(self):
        assert resolve_iso(110, ISO_ALL, nearest=True).value == 100

    def test_nearest_never_picks_bulb(self):
        assert resolve_tv(60.0, TV_M6, nearest=True).code == 0x10  # 30", not Bulb

    def test_bulb_supported(self):
        assert resolve_tv("bulb", TV_M6).is_bulb

    def test_bulb_unsupported_cannot_snap(self):
        no_bulb = [c for c in TV_M6 if c != 0x0C]
        with pytest.raises(ValueError):
            resolve_tv("bulb", no_bulb)
        with pytest.raises(ValueError):
            resolve_tv("bulb", no_bulb, nearest=True)

    def test_empty_supported_uses_full_table(self):
        assert resolve_av(1.0, ()).code == 0x08
        assert resolve_tv("1/10000", ()).code == 0xA3

    def test_out_of_table_strict(self):
        with pytest.raises(ValueError):
            resolve_av(100.0, AV_M6)

    def test_out_of_table_nearest(self):
        assert resolve_av(100.0, AV_M6, nearest=True).code == 0x50  # f/22


class TestDensotenCompat:
    """下流 Densoten リポジトリが TOML Config から渡す文字列形式の互換性。"""

    def test_config_strings(self):
        assert resolve_av("5", AV_M6).code == 0x2D  # 表示は "5.0"
        assert resolve_av("8", AV_M6).code == 0x38
        assert resolve_tv("1/30", TV_M6).code == 0x60
        assert resolve_tv("1/15", TV_M6).code == 0x58
        assert resolve_iso("500", ISO_ALL).value == 500
        assert resolve_iso("400", ISO_ALL).value == 400

    def test_profile_roundtrip_all_display_strings(self):
        # get_properties() が出力する全表示文字列を load_profile が再パースできる
        for aperture in APERTURES:
            parsed = Aperture.from_any(aperture.display)
            assert parsed.f_number == pytest.approx(aperture.f_number)
        for speed in SHUTTER_SPEEDS:
            parsed = ShutterSpeed.from_any(speed.display)
            if speed.seconds is None:
                assert parsed.is_bulb
            else:
                assert parsed.seconds == pytest.approx(speed.seconds)
        for iso in ISO_SPEEDS:
            assert ISOSpeed.from_any(iso.display).value == iso.value
