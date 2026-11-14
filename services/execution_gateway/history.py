"""Immutable account-scoped deal evidence; never sends orders or clears claims.

FIFO allocations are analytical attribution, not a broker assertion of strategy
ownership. Missing opening history and ambiguous position identities fail closed.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from packages.persistence.db import Database


class HistoryError(ValueError):
    """History cannot be certified without operator reconciliation."""


class DealEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    deal_id: str = Field(min_length=1, max_length=128)
    order_id: str = Field(min_length=1, max_length=128)
    position_id: str = Field(min_length=1, max_length=128)
    broker_id: str = Field(min_length=1, max_length=128)
    account_key: str = Field(min_length=1, max_length=256)
    currency: str = Field(min_length=1, max_length=16)
    symbol: str = Field(min_length=1, max_length=128)
    side: Literal["buy", "sell"]
    entry: Literal["in", "out", "inout", "out_by"]
    volume: float = Field(gt=0)
    price: float = Field(gt=0)
    time_msc: int = Field(ge=0, strict=True)
    profit: float = 0
    commission: float = 0
    swap: float = 0
    fee: float = 0

    @field_validator("deal_id", "order_id", "position_id", "broker_id", "account_key", "currency", "symbol")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise ValueError("blank or padded identity")
        return value


class DealLedger:
    def __init__(self, db: Database) -> None:
        self.db = db

    def ingest(self, broker_id: str, account_key: str, rows: list[dict]) -> int:
        try:
            deals = [DealEvidence(**row) for row in rows]
        except (ValidationError, TypeError) as exc:
            raise HistoryError("invalid deal evidence") from exc
        if any(d.broker_id != broker_id or d.account_key != account_key for d in deals):
            raise HistoryError("account identity mismatch")
        inserted = 0
        with self.db.cursor() as cur:
            for deal in deals:
                payload = json.dumps(deal.model_dump(), sort_keys=True, allow_nan=False)
                key = (broker_id, account_key, deal.deal_id)
                prior = cur.execute("SELECT payload FROM broker_deals WHERE broker_id=? AND account_key=? AND deal_id=?", key).fetchone()
                if prior:
                    if prior["payload"] != payload:
                        raise HistoryError("immutable deal conflict")
                    continue
                cur.execute("INSERT INTO broker_deals VALUES(?,?,?,?,?)", (*key, deal.time_msc, payload))
                inserted += 1
        return inserted

    def deals(self, broker_id: str, account_key: str) -> list[dict]:
        rows = self.db.query("SELECT payload FROM broker_deals WHERE broker_id=? AND account_key=? ORDER BY time_msc, deal_id", (broker_id, account_key))
        return [json.loads(row["payload"]) for row in rows]

    def report(self, broker_id: str, account_key: str) -> dict:
        groups = defaultdict(list)
        for row in self.deals(broker_id, account_key):
            groups[row["position_id"]].append(row)
        allocations, open_lots, issues = [], [], []
        for position_id, rows in groups.items():
            try:
                closed, remaining = self._position(rows)
                allocations.extend(closed)
                open_lots.extend(remaining)
            except HistoryError as exc:
                issues.append({"position_id": position_id, "reason": str(exc)})
        return {"ok": not issues, "manual_reconciliation_required": bool(issues),
                "allocations": allocations, "open_lots": open_lots, "issues": issues,
                "attribution": "FIFO analytical allocation; not strategy ownership",
                "auto_resolved": False}

    @staticmethod
    def _position(rows: list[dict]) -> tuple[list, list]:
        if len({(r["symbol"], r["currency"]) for r in rows}) != 1:
            raise HistoryError("position symbol/currency identity changed")
        if len({r["time_msc"] for r in rows}) != len(rows):
            raise HistoryError("same-millisecond deal ordering ambiguous")
        lots, closed = [], []
        for row in rows:
            volume = Decimal(str(row["volume"]))
            costs = row["commission"] + row["swap"] + row["fee"]
            if row["entry"] == "in":
                if row["profit"] != 0 or any(lot["side"] != row["side"] for lot in lots):
                    raise HistoryError("opening deal contradicts position history")
                lots.append(dict(row, remaining=volume, remaining_cost=costs))
                continue
            opposite = [lot for lot in lots if lot["side"] != row["side"]]
            available = sum((lot["remaining"] for lot in opposite), Decimal(0))
            if available <= 0 or len(opposite) != len(lots):
                raise HistoryError("missing opening deals or incompatible direction")
            closing = min(volume, available)
            residual = volume - closing
            if row["entry"] != "inout" and residual > 0:
                raise HistoryError("exit exceeds known opening volume")
            if row["entry"] == "inout" and residual <= 0:
                raise HistoryError("reversal lacks residual opening volume")
            left = closing
            for lot in opposite:
                amount = min(left, lot["remaining"])
                if amount <= 0:
                    break
                entry_cost = lot["remaining_cost"] * float(amount / lot["remaining"])
                # Swap is accrued on the closed exposure, not the newly opened
                # residual of a reversal. Commission/fee follow total deal volume.
                exit_cost = ((row["commission"] + row["fee"]) * float(amount / volume)
                             + row["swap"] * float(amount / closing))
                profit = row["profit"] * float(amount / closing)
                closed.append({"position_id": row["position_id"], "open_deal_id": lot["deal_id"],
                               "close_deal_id": row["deal_id"], "symbol": row["symbol"],
                               "currency": row["currency"], "volume": float(amount),
                               "entry_price": lot["price"], "exit_price": row["price"],
                               "gross_pnl": profit, "signed_costs": entry_cost + exit_cost,
                               "net_pnl": profit + entry_cost + exit_cost})
                lot["remaining_cost"] -= entry_cost
                lot["remaining"] -= amount
                left -= amount
            lots = [lot for lot in lots if lot["remaining"] > 0]
            if residual > 0:
                lots.append(dict(row, remaining=residual,
                                 remaining_cost=(row["commission"] + row["fee"]) * float(residual / volume)))
        return closed, [{"position_id": lot["position_id"], "deal_id": lot["deal_id"],
                         "symbol": lot["symbol"], "currency": lot["currency"], "side": lot["side"],
                         "volume": float(lot["remaining"]), "entry_price": lot["price"],
                         "unallocated_signed_costs": lot["remaining_cost"]} for lot in lots]

    def order_evidence(self, broker_id: str, account_key: str, order_id: str,
                       symbol: str, side: str, requested_volume: float) -> dict:
        """Inspect *opening* fill fragments for one exact broker order ID.

        A closing or reversal deal with that ID cannot be counted as a new
        opening fill, even if its direction happens to match the request.
        Neither a full matched volume nor a position ID certifies ownership
        of an internal strategy order; the caller must reconcile broker IDs.
        """
        if not math.isfinite(requested_volume) or requested_volume <= 0:
            raise HistoryError("invalid requested volume")
        rows = [r for r in self.deals(broker_id, account_key) if r["order_id"] == order_id]
        openings = [r for r in rows if r["entry"] == "in"]
        non_opening = any(r["entry"] != "in" for r in rows)
        volume = sum((Decimal(str(r["volume"])) for r in openings), Decimal(0))
        target = Decimal(str(requested_volume))
        mismatch = any(r["symbol"] != symbol or r["side"] != side for r in rows)
        position_ids = sorted({r["position_id"] for r in openings})
        # One broker order spanning multiple position IDs needs independent
        # account-mode/history reconciliation; never certify a simple fill.
        status = ("ambiguous_non_opening_deal" if non_opening else
                  "mismatch" if mismatch or volume > target else
                  "ambiguous_multiple_positions" if len(position_ids) > 1 else
                  "missing" if not openings else
                  "full" if volume == target else "partial")
        return {"status": status, "volume": float(volume),
                "deal_ids": [r["deal_id"] for r in openings],
                "ignored_non_opening_ids": [r["deal_id"] for r in rows if r["entry"] != "in"],
                "position_ids": position_ids,
                "average_price": (sum(r["price"] * r["volume"] for r in openings)
                                  / float(volume) if volume else None),
                "attribution": "broker_order_id_match_only", "auto_resolved": False}
