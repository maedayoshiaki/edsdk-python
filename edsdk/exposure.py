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
        return ("entry", value, value.display)
    if isinstance(value, bool):
        raise ValueError(f"Cannot parse ISO value {value!r}")
    if isinstance(value, int):
        if value == 0:
            return ("entry", _ISO_AUTO, "Auto")
        return ("numeric", _iso_ev(value), f"{value}")
    if isinstance(value, float):
        if value.is_integer():
            return _parse_iso_value(int(value))
        raise ValueError(f"ISO value must be an integer, got {value!r}")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("auto", "isoauto"):
            return ("entry", _ISO_AUTO, "Auto")
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

            Note: Numeric input within 0.25 EV of a table entry is treated
            as that entry even when ``nearest=False``. ISO matching (see
            :func:`resolve_iso`) requires an exact value.

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
    """Resolve *value* to a :class:`ShutterSpeed`. See :func:`resolve_av`.

    Note: Numeric input within 0.25 EV of a table entry is treated as that
    entry even when ``nearest=False``.
    """
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
