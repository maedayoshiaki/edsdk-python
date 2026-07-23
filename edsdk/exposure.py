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
