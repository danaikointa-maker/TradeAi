"""History reconciliation is read-only with respect to execution and claims."""
import pytest

from packages.persistence.db import Database


def deal(ticket="1", **patch):
    return dict(deal_id=ticket, order_id=f"order-{ticket}", position_id="position-1",
                broker_id="demo", account_key="Demo-Server:123", currency="USD",
                symbol="XAUUSD", side="buy", entry="in", volume=0.1, price=2000.0,
                time_msc=int(ticket) * 1000, profit=0.0, commission=-1.0, swap=0.0, fee=0.0) | patch


@pytest.fixture
def ledger(tmp_path):
    from services.execution_gateway.history import DealLedger

    db = Database(str(tmp_path / "history.sqlite3"))
    yield DealLedger(db), db
    db.close()


def test_immutable_scoped_deals_survive_restart(ledger):
    from services.execution_gateway.history import DealLedger, HistoryError

    service, db = ledger
    assert service.ingest("demo", "Demo-Server:123", [deal()]) == 1
    assert service.ingest("demo", "Demo-Server:123", [deal()]) == 0
    with pytest.raises(HistoryError, match="conflict"):
        service.ingest("demo", "Demo-Server:123", [deal(price=2001)])
    assert len(DealLedger(db).deals("demo", "Demo-Server:123")) == 1
    assert service.deals("demo", "another-account") == []


@pytest.mark.parametrize("patch", [{"volume": 0}, {"price": float("nan")},
                                   {"profit": float("inf")}, {"currency": ""},
                                   {"account_key": "other"}, {"broker_id": "other"},
                                   {"entry": "unknown"}, {"time_msc": -1}])
def test_bad_batch_is_rejected_atomically(ledger, patch):
    from services.execution_gateway.history import HistoryError

    service, _ = ledger
    with pytest.raises(HistoryError):
        service.ingest("demo", "Demo-Server:123", [deal(), deal("2", **patch)])
    assert service.deals("demo", "Demo-Server:123") == []


def test_partial_exits_fifo_and_costs(ledger):
    service, _ = ledger
    service.ingest("demo", "Demo-Server:123", [deal(),
        deal("2", volume=0.2, price=2010, commission=-2),
        deal("3", side="sell", entry="out", volume=0.15, price=2020, profit=250,
             commission=-1.5, swap=-0.5)])
    report = service.report("demo", "Demo-Server:123")
    assert report["ok"] is True
    allocations = report["allocations"]
    assert [a["open_deal_id"] for a in allocations] == ["1", "2"]
    assert sum(a["volume"] for a in allocations) == pytest.approx(0.15)
    assert sum(a["net_pnl"] for a in allocations) == pytest.approx(246.5)
    assert report["open_lots"][0]["volume"] == pytest.approx(0.15)


def test_netting_reversal_opens_only_residual(ledger):
    service, _ = ledger
    service.ingest("demo", "Demo-Server:123", [deal(), deal("2", side="sell", entry="inout",
        volume=0.3, price=2010, profit=100, commission=-3)])
    report = service.report("demo", "Demo-Server:123")
    assert report["ok"] is True
    assert report["open_lots"][0]["side"] == "sell"
    assert report["open_lots"][0]["volume"] == pytest.approx(0.2)
    assert report["allocations"][0]["net_pnl"] == pytest.approx(98)


def test_reversal_swap_belongs_only_to_closed_volume(ledger):
    service, _ = ledger
    service.ingest("demo", "Demo-Server:123", [deal(), deal("2", side="sell", entry="inout",
        volume=0.3, price=2010, profit=100, commission=-3, swap=-6)])
    report = service.report("demo", "Demo-Server:123")
    assert report["allocations"][0]["net_pnl"] == pytest.approx(92)
    assert report["open_lots"][0]["unallocated_signed_costs"] == pytest.approx(-2)


@pytest.mark.parametrize("rows", [[deal(entry="out", side="sell")],
    [deal(), deal("2", side="sell", entry="out", volume=0.2)],
    [deal(), deal("2", symbol="EURUSD")],
    [deal(), deal("2", currency="EUR")],
    [deal(), deal("2", side="sell", entry="in")]])
def test_incomplete_or_ambiguous_position_history_not_certified(ledger, rows):
    service, _ = ledger
    service.ingest("demo", "Demo-Server:123", rows)
    report = service.report("demo", "Demo-Server:123")
    assert report["ok"] is False
    assert report["manual_reconciliation_required"] is True
    assert report["allocations"] == []


def test_order_evidence_aggregates_partial_fills_without_resolving_claim(ledger):
    service, db = ledger
    service.ingest("demo", "Demo-Server:123", [deal(order_id="order-1", volume=0.04),
        deal("2", order_id="order-1", volume=0.06, price=2010)])
    report = service.order_evidence("demo", "Demo-Server:123", "order-1", "XAUUSD", "buy", 0.1)
    assert report["status"] == "full"
    assert report["average_price"] == pytest.approx(2006)
    assert report["auto_resolved"] is False
    assert service.order_evidence("demo", "Demo-Server:123", "order-1", "XAUUSD", "buy", 0.2)["status"] == "partial"
    assert service.order_evidence("demo", "Demo-Server:123", "order-1", "EURUSD", "buy", 0.1)["status"] == "mismatch"
    assert db.query("SELECT * FROM orders") == []


@pytest.mark.parametrize("entry", ["out", "out_by", "inout"])
def test_close_and_netting_reversal_cannot_inflate_open_order_fill(ledger, entry):
    service, _ = ledger
    service.ingest("demo", "Demo-Server:123", [
        deal("1", order_id="broker-order", volume=0.04),
        deal("2", order_id="broker-order", entry=entry, side="sell", volume=0.06,
             price=2010, profit=12),
    ])
    result = service.order_evidence("demo", "Demo-Server:123", "broker-order",
                                    "XAUUSD", "buy", 0.1)
    assert result["status"] == "ambiguous_non_opening_deal"
    assert result["volume"] == pytest.approx(0.04)
    assert result["deal_ids"] == ["1"]
    assert result["ignored_non_opening_ids"] == ["2"]
    assert result["auto_resolved"] is False


def test_one_order_multiple_broker_positions_requires_manual_reconciliation(ledger):
    service, _ = ledger
    service.ingest("demo", "Demo-Server:123", [
        deal("1", order_id="shared-order", position_id="position-a", volume=0.04),
        deal("2", order_id="shared-order", position_id="position-b", volume=0.06),
    ])
    result = service.order_evidence("demo", "Demo-Server:123", "shared-order",
                                    "XAUUSD", "buy", 0.1)
    assert result["status"] == "ambiguous_multiple_positions"
    assert result["position_ids"] == ["position-a", "position-b"]
    assert result["volume"] == pytest.approx(0.1)
    assert result["auto_resolved"] is False


def test_multiple_fragments_same_position_still_aggregate_without_approval(ledger):
    service, _ = ledger
    service.ingest("demo", "Demo-Server:123", [
        deal("1", order_id="same-order", position_id="position-a", volume=0.04),
        deal("2", order_id="same-order", position_id="position-a", volume=0.06),
    ])
    result = service.order_evidence("demo", "Demo-Server:123", "same-order",
                                    "XAUUSD", "buy", 0.1)
    assert result["status"] == "full"
    assert result["position_ids"] == ["position-a"]
    assert result["auto_resolved"] is False
