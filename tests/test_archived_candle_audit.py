"""Deterministic, offline tests for archived M5 gap auditing."""
import pytest

from packages.market_structure.archived_candle_audit import audit_archived_candles


def bar(t, o=100.0, h=102.0, l=99.0, c=101.0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def test_gap_is_visible_and_never_marked_complete():
    result = audit_archived_candles([bar(600), bar(0), bar(1200)])
    assert result["gaps"] == [
        {"after_open_utc": 0, "before_open_utc": 600, "missing_bars": 1},
        {"after_open_utc": 600, "before_open_utc": 1200, "missing_bars": 1},
    ]
    assert result["complete"] is False
    assert result["broker_tick_verified"] is False


def test_contiguous_observations_are_not_broker_proof():
    result = audit_archived_candles([bar(300), bar(0)])
    assert result["observed_contiguous"] is True
    assert result["gaps"] == []
    assert result["complete"] is False


def test_identical_duplicate_is_counted_without_inventing_bars():
    result = audit_archived_candles([bar(0), bar(0)])
    assert result["observed_bars"] == 1
    assert result["duplicate_identical_bars"] == 1


@pytest.mark.parametrize("bars", [
    [bar(0), bar(0, c=100.5)],
    [bar(301)],
    [bar(0, h=98)],
    [bar(0, c=float("nan"))],
])
def test_invalid_or_conflicting_evidence_rejected(bars):
    with pytest.raises(ValueError):
        audit_archived_candles(bars)


def test_empty_evidence_is_not_complete():
    result = audit_archived_candles([])
    assert result["observed_bars"] == 0
    assert result["observed_contiguous"] is False
    assert result["complete"] is False
