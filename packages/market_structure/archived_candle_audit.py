"""Pure, read-only continuity audit for *archived* M5 candle observations.

No broker history is downloaded or inferred. Missing intervals remain gaps.
This module does not authorize execution or certify broker-tick completeness.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any


def audit_archived_candles(
    candles: Sequence[Mapping[str, Any]],
    *,
    timeframe_seconds: int = 300,
) -> dict[str, Any]:
    """Validate timestamp/price evidence and enumerate unobserved intervals.

    Input bars must contain Unix UTC open time `t` and OHLC `o,h,l,c`.
    The result is descriptive; `complete` is always False because observed
    bars cannot prove full broker-side coverage, even when contiguous.
    """
    if timeframe_seconds <= 0:
        raise ValueError("timeframe_seconds must be positive")
    parsed: list[tuple[int, tuple[float, ...]]] = []
    for bar in candles:
        raw_t = bar["t"]
        if isinstance(raw_t, bool) or not isinstance(raw_t, int):
            raise ValueError("candle timestamp must be integer Unix seconds")
        if raw_t < 0 or raw_t % timeframe_seconds:
            raise ValueError("candle timestamp must be aligned to timeframe")
        values = tuple(float(bar[k]) for k in ("o", "h", "l", "c"))
        if not all(isfinite(v) for v in values):
            raise ValueError("nonfinite candle price")
        o, h, l, c = values
        if l > h or not (l <= o <= h and l <= c <= h):
            raise ValueError("invalid OHLC candle")
        parsed.append((raw_t, values))
    parsed.sort(key=lambda item: item[0])
    seen: dict[int, tuple[float, ...]] = {}
    for t, values in parsed:
        if t in seen and seen[t] != values:
            raise ValueError("conflicting archived candle at same timestamp")
        seen[t] = values
    timestamps = sorted(seen)
    gaps: list[dict[str, int]] = []
    for left, right in zip(timestamps, timestamps[1:]):
        if right - left > timeframe_seconds:
            gaps.append({
                "after_open_utc": left,
                "before_open_utc": right,
                "missing_bars": (right - left) // timeframe_seconds - 1,
            })
    return {
        "observed_bars": len(timestamps),
        "duplicate_identical_bars": len(parsed) - len(timestamps),
        "first_open_utc": timestamps[0] if timestamps else None,
        "last_open_utc": timestamps[-1] if timestamps else None,
        "gaps": gaps,
        "observed_contiguous": bool(timestamps) and not gaps,
        "complete": False,
        "broker_tick_verified": False,
    }
