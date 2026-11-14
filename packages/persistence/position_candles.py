"""Append-only per-position *observed* M5 candles, never fabricated fills.

These bars are archived during an owned runtime's normal observation cycle;
they are not broker tick-history certification or an excuse to backfill a
missing past with current chart levels. Repeated bars must be byte-identical.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from packages.market_structure.archived_candle_audit import audit_archived_candles
from packages.persistence.db import Database
from packages.shared_schemas.models import Position


class PositionCandleLog:
    TIMEFRAME_SECONDS = 300
    MAX_OBSERVATION_LAG_SECONDS = 900

    def __init__(self, db: Database):
        self.db = db

    def capture(self, *, position: Position, account_identity: dict,
                bars: list[dict], market_time: int, feed_mode: str) -> int:
        if (not isinstance(account_identity, dict)
                or account_identity.get("workspace_id") != self.db.workspace_id
                or account_identity.get("broker_id") != position.broker_id
                or not isinstance(feed_mode, str) or not feed_mode.strip()
                or feed_mode != feed_mode.strip()
                or not isinstance(market_time, int) or isinstance(market_time, bool)
                or market_time <= 0 or not isinstance(bars, list)):
            raise ValueError("position candle account/market source is unverified")

        prepared: list[tuple[int, str]] = []
        for bar in bars:
            if not isinstance(bar, dict) or set(bar) != {"t", "o", "h", "l", "c", "v"}:
                raise ValueError("invalid observed candle structure")
            stamp = bar["t"]
            if (not isinstance(stamp, int) or isinstance(stamp, bool)
                    or stamp < 0 or stamp % self.TIMEFRAME_SECONDS):
                raise ValueError("invalid observed candle timestamp")
            prices = (bar["o"], bar["h"], bar["l"], bar["c"], bar["v"])
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in prices):
                raise ValueError("invalid observed candle values")
            o, h, l, c, v = (float(value) for value in prices)
            if (min(o, h, l, c) <= 0 or v < 0 or h < l
                    or not l <= o <= h or not l <= c <= h):
                raise ValueError("invalid observed OHLC geometry")
            closed_at = stamp + self.TIMEFRAME_SECONDS
            # No pre-entry, still-forming, future or ancient post-hoc backfill.
            if (stamp < position.open_time or closed_at > market_time
                    or market_time - closed_at > self.MAX_OBSERVATION_LAG_SECONDS):
                continue
            payload = {"schema_version": 1, "account_identity": account_identity,
                       "symbol": position.symbol, "open_time": position.open_time,
                       "source": "runtime_closed_bar_observation", "feed_mode": feed_mode,
                       "bar": {"t": stamp, "o": o, "h": h, "l": l, "c": c, "v": v},
                       "broker_tick_verified": False}
            prepared.append((stamp, json.dumps(payload, sort_keys=True, allow_nan=False)))

        count = 0
        with self.db.cursor() as cur:
            cur.execute("BEGIN IMMEDIATE")
            for stamp, payload in prepared:
                key = (position.broker_id, position.ticket, "M5", stamp)
                old = cur.execute(
                    "SELECT payload FROM position_candles "
                    "WHERE broker_id=? AND ticket=? AND timeframe=? AND t=?", key).fetchone()
                if old:
                    if old["payload"] != payload:
                        raise ValueError("position candle provenance/values conflict")
                    continue
                cur.execute(
                    "INSERT INTO position_candles "
                    "(broker_id,ticket,timeframe,t,symbol,captured_at,payload) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (*key, position.symbol, time.time(), payload))
                count += 1
        return count

    def for_ticket(self, *, broker_id: str, ticket: str, account_identity: dict,
                   symbol: str) -> dict[str, Any]:
        empty = {"bars": [], "source": "runtime_closed_bar_observation",
                 "broker_tick_verified": False, "complete": False}
        if (not isinstance(account_identity, dict)
                or account_identity.get("broker_id") != broker_id
                or account_identity.get("workspace_id") != self.db.workspace_id):
            return empty
        collected = []
        for row in self.db.query(
                "SELECT * FROM position_candles WHERE broker_id=? AND ticket=? "
                "AND timeframe='M5' ORDER BY t", (broker_id, ticket)):
            try:
                payload = json.loads(row["payload"])
                if (payload.get("schema_version") != 1
                        or payload.get("account_identity") != account_identity
                        or row["symbol"] != symbol
                        or payload.get("symbol") != symbol
                        or payload.get("source") != "runtime_closed_bar_observation"
                        or payload.get("broker_tick_verified") is not False
                        or payload["bar"]["t"] != row["t"]):
                    return empty  # mixed or tampered evidence cannot be shown partially
                collected.append({**payload["bar"], "feed_mode": payload["feed_mode"],
                                  "captured_at": row["captured_at"]})
            except (TypeError, KeyError, ValueError, AttributeError):
                return empty
        try:
            continuity = audit_archived_candles(collected, timeframe_seconds=self.TIMEFRAME_SECONDS)
        except (KeyError, TypeError, ValueError, OverflowError):
            return empty  # corrupt evidence must not be presented as validated continuity
        return {**empty, "bars": collected, "continuity": continuity}
