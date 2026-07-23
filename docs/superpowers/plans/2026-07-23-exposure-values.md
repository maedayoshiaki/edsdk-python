# 露出値(Av/Tv/ISO)柔軟設定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Av/Tv/ISO を値オブジェクト(Aperture/ShutterSpeed/ISOSpeed)で統一的に扱い、カメラ実対応値への strict/nearest 解決を提供する。

**Architecture:** 新モジュール `edsdk/exposure.py` に変換テーブル・値オブジェクト・解決ロジックを集約。`CameraController` は薄いラッパー(get_/set_/supported_)を追加し、`set_properties` の旧パーサー群を新ロジックに置換。Canon コード体系は 1EV = 8 の線形構造なので、最近傍探索は EV(log2)空間で行う。

**Tech Stack:** Python(venv は `.venv`、**Python 3.13**。システム python は 3.12 で pyd が読めないため必ず `.venv\Scripts\python.exe` を使う)、pytest、実機 Canon EOS M6 Mark II(USB接続済み・モードダイヤル M)。

**Spec:** `docs/superpowers/specs/2026-07-23-exposure-values-design.md`

## Global Constraints

- strict がデフォルト。丸めは `nearest=True` のオプトインのみ
- 非対応値の例外型は `ValueError` を維持(下流 Densoten 互換)
- `set_properties` のシグネチャ変更は追加のみ(`nearest: bool = False` キーワード専用)
- 旧パーサー(`_parse_av`/`_parse_tv`/`_parse_iso`/`_reverse_lookup`/`_tv_display_to_seconds`)が受理した文字列形式は新実装でも全て受理(スーパーセット互換)
- `list_supported()` / `get_properties()` の出力形式は変更しない
- `requires-python = ">=3.8"` — typing は `Optional`/`Union`/`List` を使用(PEP 604 の `X | Y` 型注釈は `from __future__ import annotations` 下でのみ)
- 数値は常に写真的な値(F値・秒・ISO値)として解釈。生コードは `from_code` 経由のみ
- テスト実行コマンドは常に `.venv\Scripts\python.exe -m pytest`
- コミットメッセージ末尾に以下を付与:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## File Structure

- Create: `edsdk/exposure.py` — 値オブジェクト・テーブル・解決ロジック(このタスクの中核、外部依存は `edsdk.constants.properties` のみ)
- Modify: `edsdk/camera_controller.py` — 新メソッド追加、`set_properties` 改修、旧パーサー削除
- Create: `tests/test_exposure.py` — カメラ不要のユニットテスト
- Create: `tests/test_exposure_hardware.py` — 実機統合テスト(カメラ無しでは自動 skip)
- Modify: `README.md` — 露出制御セクション追加
- Modify: `pyproject.toml` — バージョン 0.1.5 → 0.1.6

タスク依存: Task 1 → 2 → 3 → 4 は直列。Task 5 は Task 3 完了後なら Task 4 と並列可。

---

### Task 1: exposure.py — テーブル構築と値オブジェクト

**Files:**
- Create: `edsdk/exposure.py`
- Test: `tests/test_exposure.py`

**Interfaces:**
- Consumes: `edsdk.constants.properties` の `Av`(dict)、`Tv`(dict)、`ISOSpeedCamera`(IntEnum)
- Produces: `Aperture` / `ShutterSpeed` / `ISOSpeed`(frozen dataclass、`code: int` / 数値属性 / `display: str`、classmethod `from_code(code: int)`)、モジュール定数 `APERTURES: Tuple[Aperture, ...]`、`SHUTTER_SPEEDS`、`ISO_SPEEDS`。`_ev()` メソッド(Task 2 の解決ロジックが使用、Bulb/Auto は None)

- [ ] **Step 1: pytest をインストール**

```
uv pip install pytest
```

確認: `.venv\Scripts\python.exe -m pytest --version` が `pytest 7.4` 以上を表示。

- [ ] **Step 2: 失敗するテストを書く**

`tests/test_exposure.py` を新規作成:

```python
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
```

- [ ] **Step 3: テストが失敗することを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exposure.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'edsdk.exposure'`

- [ ] **Step 4: 実装**

`edsdk/exposure.py` を新規作成:

```python
"""Exposure value objects for Av / Tv / ISO.

Converts between EDSDK property codes, display strings, and photographic
numeric values (f-number, seconds, ISO value), and resolves user input
against the values actually supported by the connected camera/lens.

Canon's code space is linear in EV: one full stop = 8 code steps
(Av: 0x08=f/1 -> 0x10=f/1.4, Tv: 0x38=1s -> 0x40=1/2s,
ISO: 0x48=100 -> 0x50=200). Nearest matching is therefore performed in
EV (log2) space.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple, Union

from edsdk.constants.properties import Av as AvTable
from edsdk.constants.properties import ISOSpeedCamera
from edsdk.constants.properties import Tv as TvTable

__all__ = [
    "Aperture",
    "ShutterSpeed",
    "ISOSpeed",
    "ApertureLike",
    "ShutterSpeedLike",
    "ISOSpeedLike",
    "APERTURES",
    "SHUTTER_SPEEDS",
    "ISO_SPEEDS",
    "resolve_av",
    "resolve_tv",
    "resolve_iso",
]

_NOT_VALID = 0xFFFFFFFF

# Third-stop steps are 1/3 EV apart; a quarter-stop window uniquely
# identifies the intended entry while absorbing rounding in user input
# (e.g. 5.657 -> f/5.6).
_EV_TOLERANCE = 0.25
# ISO values are exact integers; no rounding window in strict mode.
_ISO_EV_TOLERANCE = 1e-6

_THIRD_SUFFIX = re.compile(r"\s*\(1/3\)\s*$")


def _av_ev(f_number: float) -> float:
    if f_number <= 0:
        raise ValueError(f"f-number must be positive, got {f_number!r}")
    return 2.0 * math.log2(f_number)


def _tv_ev(seconds: float) -> float:
    if seconds <= 0:
        raise ValueError(f"Exposure seconds must be positive, got {seconds!r}")
    return math.log2(seconds)


def _iso_ev(value: float) -> float:
    if value <= 0:
        raise ValueError(f"ISO value must be positive, got {value!r}")
    return math.log2(value)


def _av_display_to_f_number(display: str) -> Optional[float]:
    text = _THIRD_SUFFIX.sub("", display.strip())
    try:
        return float(text)
    except ValueError:
        return None


def _tv_display_to_seconds(display: str) -> Optional[float]:
    text = _THIRD_SUFFIX.sub("", display.strip())
    # Canon quote style: 30" -> 30s, 3"2 -> 3.2s, 0"5 -> 0.5s
    match = re.fullmatch(r'(\d+)"(\d?)', text)
    if match:
        return float(f"{match.group(1)}.{match.group(2) or '0'}")
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class Aperture:
    """One Av table entry: EDSDK code, f-number, display string."""

    code: int
    f_number: float
    display: str

    def __str__(self) -> str:
        return f"f/{self.display}"

    def _ev(self) -> Optional[float]:
        return _av_ev(self.f_number)

    @classmethod
    def from_code(cls, code: int) -> "Aperture":
        entry = _AV_BY_CODE.get(int(code))
        if entry is None:
            raise ValueError(f"Unknown Av code 0x{int(code):X}")
        return entry

    @classmethod
    def from_any(cls, value: "ApertureLike") -> "Aperture":
        """Parse a number (f-number), string, or Aperture into a table entry."""
        return resolve_av(value)


@dataclass(frozen=True)
class ShutterSpeed:
    """One Tv table entry. ``seconds`` is None for Bulb."""

    code: int
    seconds: Optional[float]
    display: str

    def __str__(self) -> str:
        return self.display

    @property
    def is_bulb(self) -> bool:
        return self.seconds is None

    def _ev(self) -> Optional[float]:
        return None if self.seconds is None else _tv_ev(self.seconds)

    @classmethod
    def from_code(cls, code: int) -> "ShutterSpeed":
        entry = _TV_BY_CODE.get(int(code))
        if entry is None:
            raise ValueError(f"Unknown Tv code 0x{int(code):X}")
        return entry

    @classmethod
    def from_any(cls, value: "ShutterSpeedLike") -> "ShutterSpeed":
        """Parse a number (seconds), string, or ShutterSpeed into a table entry."""
        return resolve_tv(value)


@dataclass(frozen=True)
class ISOSpeed:
    """One ISO table entry. ``value`` is None for Auto."""

    code: int
    value: Optional[int]
    display: str

    def __str__(self) -> str:
        return f"ISO {self.display}"

    @property
    def is_auto(self) -> bool:
        return self.value is None

    def _ev(self) -> Optional[float]:
        return None if self.value is None else _iso_ev(self.value)

    @classmethod
    def from_code(cls, code: int) -> "ISOSpeed":
        entry = _ISO_BY_CODE.get(int(code))
        if entry is None:
            raise ValueError(f"Unknown ISO code 0x{int(code):X}")
        return entry

    @classmethod
    def from_any(cls, value: "ISOSpeedLike") -> "ISOSpeed":
        """Parse a number (ISO value, 0 = Auto), string, or ISOSpeed into a table entry."""
        return resolve_iso(value)


ApertureLike = Union[Aperture, str, int, float]
ShutterSpeedLike = Union[ShutterSpeed, str, int, float]
ISOSpeedLike = Union[ISOSpeed, str, int]


def _build_apertures() -> Tuple[Aperture, ...]:
    entries = []
    for code, display in AvTable.items():
        if code == _NOT_VALID:
            continue
        f_number = _av_display_to_f_number(display)
        if f_number is None:
            continue
        entries.append(Aperture(code=code, f_number=f_number, display=display))
    return tuple(sorted(entries, key=lambda e: e.code))


def _build_shutter_speeds() -> Tuple[ShutterSpeed, ...]:
    entries = []
    for code, display in TvTable.items():
        if code == _NOT_VALID:
            continue
        if display.strip().lower() == "bulb":
            entries.append(ShutterSpeed(code=code, seconds=None, display=display))
            continue
        seconds = _tv_display_to_seconds(display)
        if seconds is None:
            continue
        entries.append(ShutterSpeed(code=code, seconds=seconds, display=display))
    return tuple(sorted(entries, key=lambda e: e.code))


def _build_iso_speeds() -> Tuple[ISOSpeed, ...]:
    entries = []
    for name, member in ISOSpeedCamera.__members__.items():
        code = int(member)
        if code == _NOT_VALID:
            continue
        if name == "ISOAuto":
            entries.append(ISOSpeed(code=code, value=None, display="Auto"))
        elif name.startswith("ISO"):
            value = int(name[3:])
            entries.append(ISOSpeed(code=code, value=value, display=str(value)))
    return tuple(sorted(entries, key=lambda e: e.code))


APERTURES: Tuple[Aperture, ...] = _build_apertures()
SHUTTER_SPEEDS: Tuple[ShutterSpeed, ...] = _build_shutter_speeds()
ISO_SPEEDS: Tuple[ISOSpeed, ...] = _build_iso_speeds()

_AV_BY_CODE = {e.code: e for e in APERTURES}
_TV_BY_CODE = {e.code: e for e in SHUTTER_SPEEDS}
_ISO_BY_CODE = {e.code: e for e in ISO_SPEEDS}

_TV_BULB = next(e for e in SHUTTER_SPEEDS if e.seconds is None)
_ISO_AUTO = next(e for e in ISO_SPEEDS if e.value is None)
```

注意: `from_any` は Task 2 で定義する `resolve_*` を呼ぶが、呼び出し時解決なので Task 1 の時点で定義してよい(Task 1 のテストは `from_any` を呼ばない)。`resolve_av` 等がまだ存在しないため、Task 1 の時点では `from_any` を呼ぶと `NameError` になるが問題ない。

- [ ] **Step 5: テストが通ることを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exposure.py -v`
Expected: 8 passed

- [ ] **Step 6: コミット**

```
git add edsdk/exposure.py tests/test_exposure.py
git commit -m "add: 露出値オブジェクト(Aperture/ShutterSpeed/ISOSpeed)とテーブル変換を追加"
```

---

### Task 2: exposure.py — from_any / resolve(strict・nearest)

**Files:**
- Modify: `edsdk/exposure.py`(末尾に追記)
- Test: `tests/test_exposure.py`(末尾に追記)

**Interfaces:**
- Consumes: Task 1 の全定義
- Produces:
  - `resolve_av(value: ApertureLike, supported_codes: Sequence[int] = (), *, nearest: bool = False) -> Aperture`
  - `resolve_tv(value: ShutterSpeedLike, supported_codes: Sequence[int] = (), *, nearest: bool = False) -> ShutterSpeed`
  - `resolve_iso(value: ISOSpeedLike, supported_codes: Sequence[int] = (), *, nearest: bool = False) -> ISOSpeed`
  - `supported_codes` が空 = テーブル全体を対象(`from_any` はこの形で委譲)
  - 非対応値: strict は `ValueError`(サポート範囲・最近傍・`nearest=True` ヒントを含むメッセージ)、nearest は EV 最近傍へ丸め。Bulb/Auto は数値を持たないため nearest でも `ValueError`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_exposure.py` の import 文に追加:

```python
from edsdk.exposure import resolve_av, resolve_iso, resolve_tv
```

末尾に追記:

```python
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
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exposure.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_av'`

- [ ] **Step 3: 実装**

`edsdk/exposure.py` の末尾に追記:

```python
# ---------------------------------------------------------------------------
# Parsing user input
# ---------------------------------------------------------------------------
# Each parser returns ("entry", table_entry, label) for explicit/exact input
# or ("numeric", target_ev, label) for photographic numeric values.
_ParseResult = Tuple[str, object, str]


def _parse_av_value(value: ApertureLike) -> _ParseResult:
    if isinstance(value, Aperture):
        return ("entry", value, str(value))
    if isinstance(value, bool):
        raise ValueError(f"Cannot parse Av value {value!r}")
    if isinstance(value, (int, float)):
        f_number = float(value)
        return ("numeric", _av_ev(f_number), f"f/{f_number:g}")
    if isinstance(value, str):
        text = value.strip()
        if text:
            matches = [e for e in APERTURES if e.display.lower() == text.lower()]
            if matches:
                entry = min(matches, key=lambda e: e.code)
                return ("entry", entry, str(entry))
            cleaned = text.lower()
            if cleaned.startswith("f/"):
                cleaned = cleaned[2:]
            elif cleaned.startswith("f") and len(cleaned) > 1:
                cleaned = cleaned[1:]
            cleaned = cleaned.strip().rstrip("f").strip()
            try:
                f_number = float(cleaned)
            except ValueError:
                raise ValueError(f"Cannot parse Av value {value!r}") from None
            return ("numeric", _av_ev(f_number), f"f/{f_number:g}")
    raise ValueError(f"Cannot parse Av value {value!r}")


def _parse_tv_value(value: ShutterSpeedLike) -> _ParseResult:
    if isinstance(value, ShutterSpeed):
        return ("entry", value, value.display)
    if isinstance(value, bool):
        raise ValueError(f"Cannot parse Tv value {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
        return ("numeric", _tv_ev(seconds), f"{seconds:g}s")
    if isinstance(value, str):
        text = value.strip()
        if text:
            low = text.lower()
            if low == "bulb":
                return ("entry", _TV_BULB, _TV_BULB.display)
            matches = [e for e in SHUTTER_SPEEDS if e.display.lower() == low]
            if matches:
                entry = min(matches, key=lambda e: e.code)
                return ("entry", entry, entry.display)
            cleaned = low
            if cleaned.endswith("sec"):
                cleaned = cleaned[:-3].strip()
            elif cleaned.endswith("s"):
                cleaned = cleaned[:-1].strip()
            if "/" in cleaned:
                num, _, den = cleaned.partition("/")
                try:
                    seconds = float(num) / float(den)
                except (ValueError, ZeroDivisionError):
                    raise ValueError(f"Cannot parse Tv value {value!r}") from None
            else:
                try:
                    seconds = float(cleaned)
                except ValueError:
                    raise ValueError(f"Cannot parse Tv value {value!r}") from None
            return ("numeric", _tv_ev(seconds), f"{seconds:g}s")
    raise ValueError(f"Cannot parse Tv value {value!r}")


def _parse_iso_value(value: ISOSpeedLike) -> _ParseResult:
    if isinstance(value, ISOSpeed):
        return ("entry", value, str(value))
    if isinstance(value, bool):
        raise ValueError(f"Cannot parse ISO value {value!r}")
    if isinstance(value, int):
        if value == 0:
            return ("entry", _ISO_AUTO, str(_ISO_AUTO))
        return ("numeric", _iso_ev(value), f"ISO {value}")
    if isinstance(value, float):
        if value.is_integer():
            return _parse_iso_value(int(value))
        raise ValueError(f"ISO value must be an integer, got {value!r}")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("auto", "isoauto"):
            return ("entry", _ISO_AUTO, str(_ISO_AUTO))
        if text.startswith("iso"):
            text = text[3:].strip()
        if text.isdigit():
            return _parse_iso_value(int(text))
    raise ValueError(f"Cannot parse ISO value {value!r}")


# ---------------------------------------------------------------------------
# Resolution against camera-supported codes
# ---------------------------------------------------------------------------


def _unsupported_message(
    kind: str,
    label: str,
    numeric_supported: List,
    target_ev: Optional[float],
    fmt: Callable,
    nearest: bool,
    has_camera: bool,
) -> str:
    if has_camera:
        lines = [f"{kind} {label} is not supported by this camera/lens."]
    else:
        lines = [f"{kind} {label} does not match any EDSDK {kind} value."]
    if numeric_supported:
        lo = min(numeric_supported, key=lambda e: e._ev())
        hi = max(numeric_supported, key=lambda e: e._ev())
        lines.append(
            f"  Supported range: {fmt(lo)} - {fmt(hi)}"
            f" ({len(numeric_supported)} values)"
        )
        if target_ev is not None:
            near = min(
                numeric_supported,
                key=lambda e: (abs(e._ev() - target_ev), e.code),
            )
            lines.append(f"  Nearest supported: {fmt(near)}")
    if not nearest and target_ev is not None:
        lines.append("  Hint: pass nearest=True to snap to the nearest supported value.")
    return "\n".join(lines)


def _resolve(
    kind: str,
    entries: Sequence,
    parsed: _ParseResult,
    supported_codes: Sequence[int],
    nearest: bool,
    tolerance_ev: float,
    fmt: Callable,
):
    tag, payload, label = parsed
    supported_set = {int(c) for c in supported_codes}
    has_camera = bool(supported_set)
    if has_camera:
        supported = [e for e in entries if e.code in supported_set]
        if not supported:
            # Descriptor returned only codes missing from our table
            supported = list(entries)
    else:
        supported = list(entries)
    supported_code_set = {e.code for e in supported}

    if tag == "entry":
        if payload.code in supported_code_set:
            return payload
        target_ev = payload._ev()
    else:
        target_ev = payload

    if target_ev is not None:
        candidates = [
            e
            for e in entries
            if e._ev() is not None and abs(e._ev() - target_ev) <= tolerance_ev
        ]
        candidates.sort(key=lambda e: (abs(e._ev() - target_ev), e.code))
        for candidate in candidates:
            if candidate.code in supported_code_set:
                return candidate

    numeric_supported = [e for e in supported if e._ev() is not None]
    if nearest and target_ev is not None and numeric_supported:
        return min(
            numeric_supported,
            key=lambda e: (abs(e._ev() - target_ev), e.code),
        )

    raise ValueError(
        _unsupported_message(
            kind, label, numeric_supported, target_ev, fmt, nearest, has_camera
        )
    )


def resolve_av(
    value: ApertureLike,
    supported_codes: Sequence[int] = (),
    *,
    nearest: bool = False,
) -> Aperture:
    """Resolve *value* to an :class:`Aperture`.

    Args:
        value: f-number, string (``"f/5.6"``, ``"5.6"``, table display), or Aperture.
        supported_codes: Camera-supported Av codes from ``GetPropertyDesc``.
            Empty means "match against the full EDSDK table".
        nearest: Snap to the nearest supported value (EV distance) instead of
            raising when *value* is unsupported.

    Raises:
        ValueError: Unparseable input, or unsupported value with ``nearest=False``.
    """
    return _resolve(
        "Av",
        APERTURES,
        _parse_av_value(value),
        supported_codes,
        nearest,
        _EV_TOLERANCE,
        lambda e: f"f/{e.display}",
    )


def resolve_tv(
    value: ShutterSpeedLike,
    supported_codes: Sequence[int] = (),
    *,
    nearest: bool = False,
) -> ShutterSpeed:
    """Resolve *value* to a :class:`ShutterSpeed`. See :func:`resolve_av`."""
    return _resolve(
        "Tv",
        SHUTTER_SPEEDS,
        _parse_tv_value(value),
        supported_codes,
        nearest,
        _EV_TOLERANCE,
        lambda e: e.display,
    )


def resolve_iso(
    value: ISOSpeedLike,
    supported_codes: Sequence[int] = (),
    *,
    nearest: bool = False,
) -> ISOSpeed:
    """Resolve *value* to an :class:`ISOSpeed`. See :func:`resolve_av`.

    Strict matching requires an exact ISO value; ``nearest=True`` snaps in
    EV space. ``0`` / ``"auto"`` selects ISO Auto.
    """
    return _resolve(
        "ISO",
        ISO_SPEEDS,
        _parse_iso_value(value),
        supported_codes,
        nearest,
        _ISO_EV_TOLERANCE,
        lambda e: f"ISO {e.display}",
    )
```

- [ ] **Step 4: テストが通ることを確認**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exposure.py -v`
Expected: 全テスト passed(Task 1 の 8 + 新規 24 前後)

- [ ] **Step 5: コミット**

```
git add edsdk/exposure.py tests/test_exposure.py
git commit -m "add: 露出値のfrom_any/resolve(strict/nearest解決)を追加"
```

---

### Task 3: CameraController 統合と旧パーサー置換

**Files:**
- Modify: `edsdk/camera_controller.py`

**Interfaces:**
- Consumes: Task 2 の `resolve_av/resolve_tv/resolve_iso`、`Aperture/ShutterSpeed/ISOSpeed`(`.code` 属性、`from_code`)
- Produces(Task 4 の実機テストが使用):
  - `CameraController.get_av() -> Aperture` / `get_tv() -> ShutterSpeed` / `get_iso() -> ISOSpeed`
  - `CameraController.set_av(value, *, nearest: bool = False) -> Aperture`(set 後に読み返した実値を返す)/ `set_tv` / `set_iso` 同様
  - `CameraController.supported_av() -> List[Aperture]` / `supported_tv` / `supported_iso`
  - `set_properties(..., nearest: bool = False)` 追加

- [ ] **Step 1: import 追加**

`edsdk/camera_controller.py` の `from edsdk.constants.properties import (...)` ブロックの直後に追加:

```python
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
```

- [ ] **Step 2: 旧パーサーを削除**

以下のモジュールレベル定義を **完全に削除**:

- `_reverse_lookup` 関数(現 204-226 行付近)
- `_AV_STR_TO_CODE = _reverse_lookup(AvTable)` と `_TV_STR_TO_CODE = _reverse_lookup(TvTable)`
- `_parse_av` 関数
- `_parse_tv` 関数
- `_tv_display_to_seconds` 関数
- `_parse_iso` 関数

**残すもの**: `_iso_code_to_string`(`get_properties`/`list_supported` が使用)、`AvTable`/`TvTable`/`ISOSpeedCamera` の import(同上)。

- [ ] **Step 3: set_properties を改修**

シグネチャに `nearest` を追加(`validate` の直前に挿入):

```python
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
```

Av/Tv/ISO のパース行(旧 `_parse_*` 呼び出し3箇所)を置換:

```python
        if av is not None:
            codes = self._get_supported_codes(PropID.Av) if validate else ()
            to_set.append((PropID.Av, resolve_av(av, codes, nearest=nearest).code))
        if tv is not None:
            codes = self._get_supported_codes(PropID.Tv) if validate else ()
            to_set.append((PropID.Tv, resolve_tv(tv, codes, nearest=nearest).code))
        if iso is not None:
            codes = self._get_supported_codes(PropID.ISOSpeed) if validate else ()
            to_set.append((PropID.ISOSpeed, resolve_iso(iso, codes, nearest=nearest).code))
```

validate ループ全体を以下に置換(Av/Tv/ISO は解決済みのためスキップ、他は既存どおり):

```python
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
```

- [ ] **Step 4: 新メソッド追加**

`get_properties` メソッドの直後(`# ---------- Profiles ----------` の前)に挿入:

```python
    # ---------- Exposure values (Av / Tv / ISO) ----------
    def _require_session(self) -> EdsObject:
        if self._cam is None:
            raise RuntimeError("Camera session not open")
        return self._cam

    def get_av(self) -> Aperture:
        """Return the current aperture as an :class:`Aperture`."""
        cam = self._require_session()
        return Aperture.from_code(int(edsdk.GetPropertyData(cam, PropID.Av, 0)))

    def get_tv(self) -> ShutterSpeed:
        """Return the current shutter speed as a :class:`ShutterSpeed`."""
        cam = self._require_session()
        return ShutterSpeed.from_code(int(edsdk.GetPropertyData(cam, PropID.Tv, 0)))

    def get_iso(self) -> ISOSpeed:
        """Return the current ISO as an :class:`ISOSpeed`."""
        cam = self._require_session()
        return ISOSpeed.from_code(
            int(edsdk.GetPropertyData(cam, PropID.ISOSpeed, 0))
        )

    def set_av(self, value: ApertureLike, *, nearest: bool = False) -> Aperture:
        """Set the aperture and return the value the camera reports back.

        Args:
            value: f-number (5.6), string ("f/5.6", "5.6"), or Aperture.
            nearest: Snap to the nearest supported value instead of raising.
        """
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
        cam = self._require_session()
        resolved = resolve_iso(
            value, self._get_supported_codes(PropID.ISOSpeed), nearest=nearest
        )
        self._log(f"Set ISO -> {resolved}")
        edsdk.SetPropertyData(cam, PropID.ISOSpeed, 0, resolved.code)
        return self.get_iso()

    def supported_av(self) -> List[Aperture]:
        """Apertures supported by the connected camera/lens."""
        self._require_session()
        return self._supported_entries(PropID.Av, Aperture.from_code)

    def supported_tv(self) -> List[ShutterSpeed]:
        """Shutter speeds supported by the connected camera."""
        self._require_session()
        return self._supported_entries(PropID.Tv, ShutterSpeed.from_code)

    def supported_iso(self) -> List[ISOSpeed]:
        """ISO speeds supported by the connected camera."""
        self._require_session()
        return self._supported_entries(PropID.ISOSpeed, ISOSpeed.from_code)

    def _supported_entries(self, pid: PropID, from_code: Callable[[int], object]) -> List:
        entries: List = []
        for code in self._get_supported_codes(pid):
            try:
                entries.append(from_code(code))
            except ValueError:
                self._log(f"Skip unknown {pid.name} code 0x{code:X}")
        return entries
```

- [ ] **Step 5: 回帰確認**

```
.venv\Scripts\python.exe -m pytest tests/test_exposure.py -q
.venv\Scripts\python.exe -c "from edsdk.camera_controller import CameraController; print('import OK')"
```

Expected: 全 passed / `import OK`

削除漏れの確認(ヒットが残っていれば削除し忘れ):

```
grep -n "_parse_av\|_parse_tv\|_parse_iso\|_reverse_lookup\|_tv_display_to_seconds" edsdk/camera_controller.py
```

Expected: ヒットなし

- [ ] **Step 6: コミット**

```
git add edsdk/camera_controller.py
git commit -m "update: CameraControllerに露出値API(set_av/set_tv/set_iso等)を統合し旧パーサーを置換"
```

---

### Task 4: 実機統合テスト(EOS M6 Mark II)

**Files:**
- Create: `tests/test_exposure_hardware.py`

**Interfaces:**
- Consumes: Task 3 の CameraController 新 API 全て
- Produces: なし(検証のみ)

**前提:** カメラ USB 接続・電源 ON・モードダイヤル M(Manual)。カメラが無い場合は自動 skip。

- [ ] **Step 1: テストを書く**

`tests/test_exposure_hardware.py` を新規作成:

```python
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
```

- [ ] **Step 2: 実機で実行**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exposure_hardware.py -v`
Expected: 9 passed(カメラ接続時)。カメラの Av/Tv/ISO はテスト後に元の値へ復元される。

失敗した場合: superpowers:systematic-debugging スキルを使って原因を特定してから修正すること(闇雲に直さない)。

- [ ] **Step 3: ユニットテストも含め全体を実行**

Run: `.venv\Scripts\python.exe -m pytest tests -v`
Expected: 全 passed

- [ ] **Step 4: コミット**

```
git add tests/test_exposure_hardware.py
git commit -m "test: 露出値APIの実機統合テストを追加"
```

---

### Task 5: README とバージョン更新

**Files:**
- Modify: `README.md`(`## Troubleshooting` セクションの直前に挿入)
- Modify: `pyproject.toml:12`

**Interfaces:**
- Consumes: Task 3 の公開 API(ドキュメント化のみ)
- Produces: なし

- [ ] **Step 1: README にセクション追加**

`README.md` の `## Troubleshooting` の直前に以下を挿入:

````markdown
## Exposure control (Av / Tv / ISO)

`CameraController` exposes camera-aware exposure APIs. Values can be given
as numbers (f-number / seconds / ISO value), strings (`"1/125"`, `"f/5.6"`,
`"auto"`), or table entries obtained from `supported_*()`.

```python
from edsdk.camera_controller import CameraController

with CameraController() as cam:
    cam.supported_av()             # lens-dependent list, e.g. [f/3.5 ... f/22]
    cam.set_av(5.6)                # strict: raises ValueError if unsupported
    cam.set_tv(1 / 125)            # returns the value the camera reports back
    cam.set_iso(400)
    cam.set_av(2.8, nearest=True)  # snaps to nearest supported (e.g. f/3.5)

    # Legacy API keeps working and uses the same engine:
    cam.set_properties(av="5", tv="1/30", iso="500", nearest=True)
```

Unsupported values raise `ValueError` listing the supported range and the
nearest candidate. Pass `nearest=True` to snap automatically (matching is
done in EV / log2 space). `Bulb` and ISO `Auto` are never chosen by
`nearest`; request them explicitly (`"bulb"` / `"auto"`).
````

- [ ] **Step 2: バージョン更新**

`pyproject.toml` 12行目:

```toml
version = "0.1.6"  # 露出値(Av/Tv/ISO)API追加バージョン
```

- [ ] **Step 3: 確認とコミット**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: 全 passed

```
git add README.md pyproject.toml
git commit -m "upgrade: READMEに露出制御を追記しバージョン番号を0.1.5から0.1.6に更新"
```
