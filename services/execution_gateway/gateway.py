"""Execution gateway: idempotent order submission, verification, reconciliation.

Local safety boundaries
-----------------------
* one retained idempotency key -> at most one adapter send attempt in this database;
* fills are checked against positions when verify_after_send is enabled;
* a broker that is not connected can never receive an order;
* every state transition is written to the audit log.

This is not broker-side exactly-once execution. A new key, a restored/deleted
database, or ambiguous broker history still needs separate operator reconciliation.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from integrations.brokers.base import BrokerAdapter, BrokerError
from packages.persistence.db import Database
from packages.persistence.position_events import PositionEventLog
from packages.shared_schemas.models import (
    Direction,
    Health,
    OrderRequest,
    OrderResult,
    OrderState,
    Position,
    TradeMode,
    TradeRecord,
)
from packages.shared_schemas.settings import Settings

POSITION_EVIDENCE_FIELDS = ("symbol", "direction", "volume", "entry", "sl", "tp", "open_time")


def _position_evidence(pos: Position | None) -> dict[str, Any] | None:
    """Stable broker fields only: exclude live mark-to-market price/noise."""
    if pos is None:
        return None
    out: dict[str, Any] = {}
    for field in POSITION_EVIDENCE_FIELDS:
        value = getattr(pos, field, None)
        out[field] = value.value if isinstance(value, Direction) else value
    return out


class ExecutionGateway:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        ex = settings.execution
        # kept for visibility; submit() never resends (see the docstring there)
        self.max_retries = int(ex.get("max_retries", 0))
        self.verify_after_send = bool(ex.get("verify_after_send", True))
        self.order_timeout = float(ex.get("order_timeout_seconds", 20))

    def _account(self, adapter: BrokerAdapter) -> dict[str, str]:
        info = adapter.account_info()
        values = {"broker_id": info.broker_id, "login": info.login, "server": info.server,
                  "currency": info.currency, "workspace_id": self.db.workspace_id}
        if (values["broker_id"] != adapter.id or any(not isinstance(v, str) or not v.strip()
                or v != v.strip() or v == "—" for v in values.values())):
            raise BrokerError("account identity unavailable")
        return values

    def _assert_account(self, adapter: BrokerAdapter) -> dict[str, str]:
        identity = self._account(adapter)
        key = "account-owner:" + adapter.id
        # Persist once per alias; never silently migrate historical resources.
        with self.db.cursor() as cur:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    owner = json.loads(row["value"])
                except (ValueError, TypeError) as exc:
                    raise BrokerError("account ownership record invalid") from exc
                if owner != identity:
                    raise BrokerError("account ownership mismatch; manual reconciliation required")
            else:
                if (cur.execute("SELECT 1 FROM orders WHERE broker_id=? LIMIT 1", (adapter.id,)).fetchone()
                        or cur.execute("SELECT 1 FROM trades WHERE broker_id=? LIMIT 1", (adapter.id,)).fetchone()
                        or cur.execute("SELECT 1 FROM open_positions LIMIT 1").fetchone()):
                    raise BrokerError("legacy resources have unknown account ownership")
                cur.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)",
                            (key, json.dumps(identity, sort_keys=True), time.time()))
        return identity

    # ----------------------------------------------------------- idempotency
    @staticmethod
    def make_key(request: OrderRequest) -> str:
        if request.idempotency_key:
            return request.idempotency_key
        raw = f"{request.broker_id or ''}:{request.symbol}:{request.side.value}:{request.volume}:{request.signal_id or ''}"
        return "idem-" + uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:20]

    def _existing(self, key: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM orders WHERE idempotency_key = ?", (key,))
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
            stored = OrderRequest(**payload["request"])
            if stored.broker_id != row["broker_id"] or stored.idempotency_key != key:
                raise ValueError("record identity mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            raise BrokerError("idempotency record invalid; manual reconciliation required") from exc
        return {"id": row["id"], "state": row["state"], "result": payload.get("result"),
                "account_identity": payload.get("account_identity"),
                "request": stored}

    def _replay(self, adapter: BrokerAdapter, request: OrderRequest,
                existing: dict[str, Any], actor: str) -> OrderResult:
        if existing["request"].model_dump() != request.model_dump():
            raise BrokerError("idempotency key already belongs to a different request")
        if existing["account_identity"] != self._account(adapter):
            raise BrokerError("idempotency account identity missing or mismatched")
        result = None
        if existing["result"] is not None:
            try:
                result = OrderResult(**existing["result"])
                if (result.order_id != existing["id"] or result.idempotency_key != request.idempotency_key
                        or result.status.value != existing["state"]):
                    raise ValueError("result identity mismatch")
            except (ValueError, TypeError) as exc:
                raise BrokerError("idempotency result invalid; manual reconciliation required") from exc
        # A position sharing signal_id, symbol and volume is NOT proof that
        # this particular order reached the broker. It could predate the send
        # or belong to another EA. Keep the persisted unknown outcome until
        # authoritative broker order/deal IDs can be reconciled explicitly.
        self.db.audit(actor, "order.idempotent_hit", {"key": request.idempotency_key})
        return result or OrderResult(
            order_id=existing["id"], idempotency_key=request.idempotency_key,
            status=OrderState.pending, message="order unresolved; not resent — manual reconciliation required",
        )

    @staticmethod
    def _validate_original_plan(snapshot: dict[str, Any] | None, request: OrderRequest) -> None:
        """Reject misattributed evidence before claiming an order, never infer old zones."""
        if snapshot is None:
            return
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("signal"), dict):
            raise BrokerError("original plan snapshot is invalid")
        signal = snapshot["signal"]
        plan = signal.get("plan")
        expected_action = "BUY" if request.side.value == "buy" else "SELL"
        expected_direction = "up" if request.side.value == "buy" else "down"
        if (not request.signal_id or signal.get("id") != request.signal_id
                or signal.get("broker_id") != request.broker_id
                or signal.get("symbol") != request.symbol
                or signal.get("action") != expected_action
                or not isinstance(plan, dict)
                or plan.get("direction") != expected_direction
                or plan.get("lot") != request.volume
                or plan.get("sl") != request.sl
                or plan.get("tp3") != request.tp):
            raise BrokerError("original plan does not match order intent")
        analysis = snapshot.get("analysis")
        if analysis is not None and (not isinstance(analysis, dict)
                                    or analysis.get("symbol") != request.symbol
                                    or analysis.get("broker_id") != request.broker_id
                                    or analysis.get("timeframe") != signal.get("timeframe")):
            raise BrokerError("original analysis does not match the plan")

    # ---------------------------------------------------------------- submit
    def submit(self, adapter: BrokerAdapter, request: OrderRequest, actor: str = "system",
               original_plan_snapshot: dict[str, Any] | None = None) -> OrderResult:
        if request.broker_id is not None and request.broker_id != adapter.id:
            raise BrokerError("broker identity mismatch; idempotency intent must not be rewritten")
        identity = self._assert_account(adapter)
        health = adapter.health()
        if health not in (Health.connected, Health.degraded):
            raise BrokerError(f"refusing to send: broker {adapter.id} is {health.value}")
        request = request.model_copy(update={"broker_id": adapter.id})
        key = self.make_key(request)
        request = request.model_copy(update={"idempotency_key": key})
        self._validate_original_plan(original_plan_snapshot, request)

        # Serialize the exact intent before broker I/O. A missing strategy plan
        # is labelled request-only, never fabricated from later market data.
        snapshot_payload = {
            "schema_version": 1, "account_identity": identity,
            "request": request.model_dump(mode="json"),
            "evidence_level": "original_plan" if original_plan_snapshot is not None else "request_only",
            "original_signal": original_plan_snapshot["signal"] if original_plan_snapshot else None,
            "analysis": original_plan_snapshot.get("analysis") if original_plan_snapshot else None,
            "risk_decision": original_plan_snapshot.get("risk_decision") if original_plan_snapshot else None,
            "analysis_provenance": (original_plan_snapshot.get("analysis_provenance", "unavailable")
                                    if original_plan_snapshot else "unavailable"),
        }
        try:
            serialized_snapshot = json.dumps(snapshot_payload, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BrokerError("original plan evidence is not serializable") from exc

        existing = self._existing(key)
        if existing:
            return self._replay(adapter, request, existing, actor)

        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
        # Serialize admission across connections to this database. Recheck both
        # the key and broker uncertainty AFTER taking the SQLite writer lock.
        # Only database operations belong here: replay/verification and broker
        # I/O must run after commit so another connection can observe the claim.
        with self.db.cursor() as cur:
            cur.execute("BEGIN IMMEDIATE")
            already_claimed = cur.execute("SELECT 1 FROM orders WHERE idempotency_key=?", (key,)).fetchone()
            if already_claimed:
                existing = self._existing(key)
            else:
                if self.unresolved_orders(adapter.id):
                    raise BrokerError("unresolved orders require reconciliation before new execution")
                cur.execute(
                    "INSERT INTO orders(id, created_at, idempotency_key, state, broker_id, symbol, payload) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (order_id, time.time(), key, OrderState.pending.value, adapter.id, request.symbol,
                     json.dumps({"request": request.model_dump(), "actor": actor,
                                 "account_identity": identity}, default=str)),
                )
                # An insert failure rolls back BOTH the order claim and the
                # evidence. A replay never rewrites the original snapshot.
                cur.execute(
                    "INSERT INTO pre_order_snapshots"
                    "(order_id, broker_id, signal_id, symbol, captured_at, payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (order_id, adapter.id, request.signal_id, request.symbol,
                     time.time(), serialized_snapshot),
                )
        if already_claimed:
            if existing is None:
                raise BrokerError("idempotency claim disappeared; refusing to send")
            return self._replay(adapter, request, existing, actor)
        self.db.audit(actor, "order.submit", request.model_dump())

        # One attempt only. A transport error does not prove the order was not
        # executed, so resending could duplicate the position — the operator (or
        # the reconciler) resolves it instead of the gateway guessing.
        try:
            if self._account(adapter) != identity:
                raise BrokerError("account identity changed before send")
            result = adapter.order_send(request, ticket_hint="")
            self._assert_account(adapter)
            result.order_id = order_id
            result.idempotency_key = key
            if result.status == OrderState.filled:
                if self.verify_after_send:
                    result.verified = self._verify_fill(adapter, result, request)
                    self._assert_account(adapter)
                    if not result.verified:
                        result.message += " | fill evidence missing or mismatched (manual reconciliation required)"
                else:
                    # A bridge's optimistic verified flag cannot override the
                    # local verification gate simply because it was disabled.
                    result.verified = False
                    result.message += " | fill verification disabled (manual reconciliation required)"
            self._persist_result(order_id, result, request)
            self.db.audit(actor, "order.result", result.model_dump())
            return result
        except BrokerError as exc:
            last_error = str(exc)
            self.db.audit(actor, "order.error", {"error": last_error, "retcode": exc.retcode,
                                                 "resent": False})
        except Exception as exc:  # noqa: BLE001
            last_error = f"unexpected: {exc}"
            self.db.audit(actor, "order.error", {"error": last_error, "resent": False})

        result = OrderResult(
            order_id=order_id, idempotency_key=key, status=OrderState.pending,
            message=(f"order not confirmed: {last_error} | not resent automatically — "
                     f"broker order/deal identity must be reconciled manually; "
                     f"do not retry with a new key"),
            created_at=time.time(),
        )
        self._persist_result(order_id, result, request)
        return result

    def _persist_result(self, order_id: str, result: OrderResult, request: OrderRequest) -> None:
        row = self.db.query_one("SELECT payload FROM orders WHERE id=?", (order_id,))
        payload = json.loads(row["payload"]) if row else {}
        payload.update(request=request.model_dump(), result=result.model_dump())
        self.db.execute(
            "UPDATE orders SET state=?, payload=? WHERE id=?",
            (result.status.value, json.dumps(payload, default=str), order_id),
        )

    def _verify_fill(self, adapter: BrokerAdapter, result: OrderResult, request: OrderRequest) -> bool:
        """Confirm one full position against both intent and broker acknowledgement.

        Partial fills and netted positions need history-aware reconciliation; a
        matching ticket alone must never certify those ambiguous outcomes.
        """
        if not result.ticket:
            return False
        try:
            values = (request.volume, result.filled_volume, result.fill_price)
            if not all(value is not None and math.isfinite(value) and value > 0 for value in values):
                return False
            matches = [p for p in adapter.positions() if p.ticket == result.ticket]
            if len(matches) != 1:
                return False
            pos = matches[0]
            return (
                pos.broker_id == adapter.id == request.broker_id
                and pos.symbol == request.symbol
                and pos.direction == (Direction.up if request.side.value == "buy" else Direction.down)
                and (not request.signal_id or pos.signal_id == request.signal_id)
                and math.isfinite(pos.volume) and pos.volume > 0
                and math.isfinite(pos.entry) and pos.entry > 0
                and math.isclose(pos.volume, request.volume, rel_tol=1e-9, abs_tol=1e-9)
                and math.isclose(pos.volume, result.filled_volume, rel_tol=1e-9, abs_tol=1e-9)
                and math.isclose(pos.entry, result.fill_price, rel_tol=1e-9, abs_tol=1e-9)
            )
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ management
    def modify(self, adapter: BrokerAdapter, ticket: str, sl: float | None = None,
               tp: float | None = None, reason: str = "") -> bool:
        try:
            identity = self._assert_account(adapter)
            ledger = PositionEventLog(self.db)
            detail = {"sl": sl, "tp": tp, "reason": reason}
            ledger.append(broker_id=adapter.id, ticket=ticket, actor="position-manager",
                          kind="modify", status="requested", account_identity=identity, detail=detail)
            ok = adapter.modify_position(ticket, sl=sl, tp=tp)
            self._assert_account(adapter)
            ledger.append(broker_id=adapter.id, ticket=ticket, actor="position-manager",
                          kind="modify", status="accepted_unverified" if ok else "unknown",
                          account_identity=identity, detail=detail)
            self.db.audit("position-manager", "position.modify",
                          {"ticket": ticket, "sl": sl, "tp": tp, "ok": ok, "reason": reason})
            return ok
        except BrokerError as exc:
            # The request already exists in append-only evidence; a transport
            # exception cannot prove that the broker rejected it.
            self.db.audit("position-manager", "position.modify_failed", {"ticket": ticket, "error": str(exc)})
            return False

    def close(self, adapter: BrokerAdapter, ticket: str, volume: float | None = None,
              reason: str = "manual", actor: str = "user") -> dict[str, Any]:
        """Close (or partially close) a position and normalise the two shapes.

        The simulated broker settles inside `close_position` and returns the
        finished ``TradeRecord``; the MT5 adapter returns the order's
        ``OrderResult``. Callers used to assume ``.status`` existed and crashed on
        every simulated close, so this method now returns one dict for both.
        """
        identity = self._assert_account(adapter)
        ledger = PositionEventLog(self.db)
        detail = {"volume": volume, "reason": reason}
        ledger.append(broker_id=adapter.id, ticket=ticket, actor=actor, kind="close",
                      status="requested", account_identity=identity, detail=detail)
        try:
            result = adapter.close_position(ticket, volume=volume, comment=reason[:24])
        except Exception:
            ledger.append(broker_id=adapter.id, ticket=ticket, actor=actor, kind="close",
                          status="unknown", account_identity=identity, detail=detail)
            raise
        self._assert_account(adapter)
        payload: dict[str, Any]
        if isinstance(result, TradeRecord):
            still_open = any(p.ticket == ticket for p in adapter.positions())
            self._assert_account(adapter)
            if still_open:
                # A partial close: the position lives on, so it must not be
                # written into the closed-trades table yet.
                self.db.audit(actor, "position.partial_realized",
                              {"ticket": ticket, "pnl": result.pnl, "volume": result.volume,
                               "reason": reason})
            else:
                self.record_trade(result)
            ledger.append(broker_id=adapter.id, ticket=ticket, actor=actor, kind="close",
                          status="partial_realized" if still_open else "closed_recorded",
                          account_identity=identity,
                          detail={**detail, "realized_volume": result.volume, "pnl": result.pnl})
            payload = {"ok": True, "trade": result.model_dump()}
        else:
            if result.status not in (OrderState.closed, OrderState.filled, OrderState.rejected, OrderState.cancelled):
                ledger.append(broker_id=adapter.id, ticket=ticket, actor=actor, kind="close",
                              status="unknown", account_identity=identity, detail=detail)
                raise BrokerError("close outcome unknown; reconcile before further action or resending")
            ok = result.status in (OrderState.closed, OrderState.filled)
            ledger.append(broker_id=adapter.id, ticket=ticket, actor=actor, kind="close",
                          status="accepted_unverified" if ok else "rejected",
                          account_identity=identity,
                          detail={**detail, "retcode": result.retcode, "broker_status": result.status.value})
            payload = {"ok": ok, "order": result.model_dump()}
        self.db.audit(actor, "position.close",
                      {"ticket": ticket, "volume": volume, "reason": reason, "result": payload})
        return payload

    def unresolved_orders(self, broker_id: str) -> list[dict[str, Any]]:
        """Expose uncertainty without sending, guessing history, or clearing claims."""
        unresolved = []
        for row in self.db.query("SELECT * FROM orders WHERE broker_id=?", (broker_id,)):
            try:
                payload = json.loads(row["payload"])
                stored = OrderRequest(**payload["request"])
                owner = self.db.query_one("SELECT value FROM kv WHERE key=?", ("account-owner:" + broker_id,))
                identity = json.loads(owner["value"]) if owner else None
                if (not isinstance(identity, dict) or identity.get("broker_id") != broker_id
                        or identity.get("workspace_id") != self.db.workspace_id
                        or payload.get("account_identity") != identity):
                    raise ValueError("account identity missing or mismatched")
                if (stored.broker_id != row["broker_id"] or stored.symbol != row["symbol"]
                        or stored.idempotency_key != row["idempotency_key"]):
                    raise ValueError("request identity mismatch")
                result = OrderResult(**payload["result"]) if payload.get("result") else None
                if result and (result.status.value != row["state"] or result.order_id != row["id"]
                               or result.idempotency_key != row["idempotency_key"]):
                    raise ValueError("result identity mismatch")
                resolved = (result is not None and
                            (result.status in (OrderState.rejected, OrderState.cancelled, OrderState.closed)
                             or (result.status == OrderState.filled and result.verified)))
            except (ValueError, TypeError, KeyError):
                resolved = False
            if not resolved:
                unresolved.append({"id": row["id"], "state": row["state"], "symbol": row["symbol"]})
        return unresolved

    def reconcile(self, adapter: BrokerAdapter) -> dict[str, Any]:
        """Compare remembered positions with the broker; never place orders here."""
        try:
            identity = self._assert_account(adapter)
            all_stored = {r["ticket"]: Position(**json.loads(r["payload"]))
                          for r in self.db.query("SELECT * FROM open_positions")}
            stored = {ticket: pos for ticket, pos in all_stored.items() if pos.broker_id == adapter.id}
            positions = adapter.positions()
            self._assert_account(adapter)
            live = {p.ticket: p for p in positions}
            if len(live) != len(positions) or any(p.broker_id != adapter.id for p in positions):
                raise BrokerError("ambiguous position identity; manual reconciliation required")
            if any(ticket in all_stored and all_stored[ticket].broker_id != adapter.id for ticket in live):
                raise BrokerError("cross-account ticket collision; isolated storage required")
            missing_at_broker = sorted(set(stored) - set(live))
            unknown_locally = sorted(set(live) - set(stored))
            changed_at_broker = sorted(ticket for ticket in set(live) & set(stored)
                                       if _position_evidence(live[ticket]) != _position_evidence(stored[ticket]))
            events = PositionEventLog(self.db)
            new_observations: list[str] = []
            observation_transitions: list[dict[str, str]] = []
            for ticket in sorted(set(live) | set(stored)):
                before, broker = _position_evidence(stored.get(ticket)), _position_evidence(live.get(ticket))
                changed_fields = ([key for key in POSITION_EVIDENCE_FIELDS if before and broker
                                   and before[key] != broker[key]]
                                  if before and broker else ["presence"])
                status = ("missing_unverified" if ticket in missing_at_broker else
                          "unexpected_unverified" if ticket in unknown_locally else
                          "changed_unverified" if ticket in changed_at_broker else
                          "baseline_restored")
                event_id = events.observe_broker(
                    broker_id=adapter.id, ticket=ticket, account_identity=identity,
                    status=status, detail={"before": before, "broker": broker,
                                           "changed_fields": changed_fields,
                                           "attribution": "unverified_source"})
                if event_id is not None:
                    new_observations.append(event_id)
                    observation_transitions.append({"ticket": ticket, "status": status})
            for ticket, pos in live.items():
                if ticket in changed_at_broker:
                    continue  # Preserve prior baseline until manual/history reconciliation.
                self.db.execute(
                    "INSERT OR REPLACE INTO open_positions(ticket, payload, updated_at) VALUES(?,?,?)",
                    (ticket, json.dumps(pos.model_dump(), default=str), time.time()),
                )
            # A missing broker position is *not* proof of a filled close; keep
            # the previous local baseline until verified deal/trade evidence.
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "checked_at": time.time()}
        status = {
            "ok": not missing_at_broker and not unknown_locally and not changed_at_broker,
            "broker_id": adapter.id,
            "positions": len(live),
            "missing_at_broker": missing_at_broker,
            "unknown_locally": unknown_locally,
            "changed_at_broker": changed_at_broker,
            "observation_events": new_observations,
            "observation_transitions": observation_transitions,
            "observations_added": len(new_observations),
            "reconciliation_required": bool(missing_at_broker or unknown_locally or changed_at_broker),
            "checked_at": time.time(),
        }
        if not status["ok"]:
            self.db.audit("reconciler", "reconcile.mismatch", status)
        return status

    # ---------------------------------------------------------------- journal
    def refresh_history(self, adapter: BrokerAdapter, date_from: float, date_to: float) -> dict[str, Any]:
        """Collect analytical history without resolving claims or sending orders."""
        from services.execution_gateway.history import DealLedger

        reader = getattr(adapter, "deal_history", None)
        if not callable(reader):
            raise BrokerError("adapter does not provide authoritative deal history")
        identity = self._assert_account(adapter)
        batch = reader(date_from, date_to)
        self._assert_account(adapter)
        expected = {key: identity[key] for key in ("broker_id", "login", "server", "currency")}
        if (not isinstance(batch, dict) or batch.get("account_identity") != expected
                or batch.get("account_key") != f"{identity['server']}:{identity['login']}"):
            raise BrokerError("history account identity mismatch or missing")
        deals = batch.get("deals")
        if (not isinstance(deals, list) or any(not isinstance(deal, dict)
                or deal.get("currency") != identity["currency"] for deal in deals)):
            raise BrokerError("history account currency evidence invalid")
        if batch["broker_id"] != adapter.id:
            raise BrokerError("history broker identity mismatch")
        ledger = DealLedger(self.db)
        inserted = ledger.ingest(adapter.id, batch["account_key"], batch["deals"])
        report = ledger.report(adapter.id, batch["account_key"])
        return {**report, "inserted": inserted, "broker_id": adapter.id,
                "account_key": batch["account_key"], "date_from": date_from, "date_to": date_to}

    def record_trade(self, trade: TradeRecord) -> None:
        payload = json.dumps(trade.model_dump(), default=str)
        with self.db.cursor() as cur:
            cur.execute("BEGIN IMMEDIATE")
            snapshot = cur.execute("SELECT payload FROM open_positions WHERE ticket=?", (trade.ticket,)).fetchone()
            existing = cur.execute("SELECT broker_id,payload FROM trades WHERE ticket=?", (trade.ticket,)).fetchone()
            if snapshot:
                position = Position(**json.loads(snapshot["payload"]))
                if position.broker_id != trade.broker_id:
                    raise BrokerError("trade snapshot account collision; manual reconciliation required")
                if (position.symbol != trade.symbol or position.direction != trade.direction
                        or position.open_time != trade.opened_at or position.entry != trade.entry):
                    raise BrokerError("trade snapshot evidence conflict; manual reconciliation required")
            if existing:
                if existing["broker_id"] != trade.broker_id or json.loads(existing["payload"]) != json.loads(payload):
                    raise BrokerError("trade evidence conflict; manual reconciliation required")
                # Identical delivery is idempotent, not permission to remove a
                # newer snapshot that happens to reuse this ticket.
                return
            cur.execute(
                "INSERT INTO trades(ticket, opened_at, closed_at, symbol, direction, volume, entry, exit, "
                "pnl, points, exit_reason, strategy, mode, broker_id, payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade.ticket, trade.opened_at, trade.closed_at, trade.symbol, trade.direction.value, trade.volume,
                 trade.entry, trade.exit, trade.pnl, trade.points, trade.exit_reason, trade.strategy,
                 trade.mode.value if isinstance(trade.mode, TradeMode) else str(trade.mode), trade.broker_id,
                 payload),
            )
            cur.execute("DELETE FROM open_positions WHERE ticket = ?", (trade.ticket,))

    def open_position_snapshot(self, positions: list[Position], broker_id: str | None = None, *,
                               account_identity: dict | None = None,
                               retain_unverified: bool = False) -> list[dict[str, str]]:
        """Replace the local mirror with the broker's current position list.

        Upsert-only snapshots leave rows behind after the last position closes,
        which then shows up as a bogus "missing at broker" mismatch.
        """
        if ((broker_id is not None and (not isinstance(broker_id, str)
                or not broker_id.strip() or broker_id != broker_id.strip()))
                or (not positions and broker_id is None)):
            raise BrokerError("snapshot requires explicit nonblank broker scope")
        if retain_unverified and (broker_id is None or not isinstance(account_identity, dict)
                                  or account_identity.get("broker_id") != broker_id
                                  or account_identity.get("workspace_id") != self.db.workspace_id):
            raise BrokerError("safe snapshot requires verified account/workspace identity")
        live_tickets = {pos.ticket for pos in positions}
        if len(live_tickets) != len(positions):
            raise BrokerError("duplicate position tickets; manual reconciliation required")
        for pos in positions:
            if broker_id is not None and pos.broker_id != broker_id:
                raise BrokerError("snapshot broker identity mismatch")
        # Serialize validation and replacement across connections to this database.
        # No broker I/O or nested Database helpers inside this transaction.
        observation_events: list[dict[str, str]] = []
        with self.db.cursor() as cur:
            cur.execute("BEGIN IMMEDIATE")
            stored = {row["ticket"]: Position(**json.loads(row["payload"]))
                      for row in cur.execute("SELECT ticket, payload FROM open_positions").fetchall()}
            for pos in positions:
                if pos.ticket in stored and stored[pos.ticket].broker_id != pos.broker_id:
                    raise BrokerError("cross-account ticket collision; isolated storage required")
            if retain_unverified:
                ledger = PositionEventLog(self.db)
                prior_scope = {ticket: pos for ticket, pos in stored.items()
                               if pos.broker_id == broker_id}
                incoming = {pos.ticket: pos for pos in positions}
                for ticket in sorted(set(prior_scope) | set(incoming)):
                    before = _position_evidence(prior_scope.get(ticket))
                    observed = _position_evidence(incoming.get(ticket))
                    status = ("missing_unverified" if ticket not in incoming else
                              "unexpected_unverified" if ticket not in prior_scope else
                              "changed_unverified" if before != observed else
                              "baseline_restored")
                    changed = ([key for key in POSITION_EVIDENCE_FIELDS if before and observed
                                and before[key] != observed[key]]
                               if before and observed else ["presence"])
                    event_id = ledger.observe_broker_in_transaction(
                        cur, broker_id=broker_id, ticket=ticket,
                        account_identity=account_identity, status=status,
                        detail={"before": before, "broker": observed,
                                "changed_fields": changed, "attribution": "unverified_source"})
                    if event_id is not None:
                        observation_events.append({"event_id": event_id, "ticket": ticket,
                                                   "status": status})
            for pos in positions:
                if (retain_unverified and pos.ticket in stored
                        and _position_evidence(stored[pos.ticket]) != _position_evidence(pos)):
                    continue  # An unexplained change cannot rewrite the baseline.
                cur.execute(
                    "INSERT OR REPLACE INTO open_positions(ticket, payload, updated_at) VALUES(?,?,?)",
                    (pos.ticket, json.dumps(pos.model_dump(), default=str), time.time()),
                )
            scope = {broker_id} if broker_id is not None else {p.broker_id for p in positions}
            for ticket, pos in stored.items():
                if ticket not in live_tickets and pos.broker_id in scope and not retain_unverified:
                    cur.execute("DELETE FROM open_positions WHERE ticket = ?", (ticket,))
        return observation_events

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            out.append(payload or dict(row))
        return out

    def open_positions(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM open_positions")
        out = []
        for row in rows:
            try:
                out.append(json.loads(row["payload"]))
            except json.JSONDecodeError:
                continue
        return out


def direction_from_string(value: str) -> Direction:
    return Direction.up if value.lower() in ("buy", "long", "up") else Direction.down
