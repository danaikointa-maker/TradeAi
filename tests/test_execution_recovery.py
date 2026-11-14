"""Repository-local crash/retry tests: no terminal or network connections."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from integrations.brokers.base import BrokerError
from packages.persistence.db import Database
from packages.shared_schemas.models import (
    Direction,
    Health,
    OrderRequest,
    OrderResult,
    OrderState,
)
from services.execution_gateway.gateway import ExecutionGateway


@pytest.fixture
def execution(tmp_path):
    path = str(tmp_path / "orders.sqlite3")
    db = Database(path)
    settings = SimpleNamespace(execution={"verify_after_send": True})
    adapter = Mock()
    adapter.id = "isolated-demo"
    adapter.account_info.return_value = SimpleNamespace(broker_id=adapter.id, login="123", server="Demo", currency="USD")
    adapter.health.return_value = Health.connected
    adapter.positions.return_value = []
    adapter.order_send.side_effect = BrokerError("transport timeout")
    ExecutionGateway(settings, db)._assert_account(adapter)
    request = OrderRequest(symbol="XAUUSD", side="buy", volume=0.1, sl=1990, tp=2020,
                           signal_id="signal-1", idempotency_key="key-1", broker_id=adapter.id)
    yield settings, db, adapter, request, path
    db.close()


@pytest.mark.parametrize("positions_fail", [False, True])
def test_restart_does_not_resend_unknown_outcome(execution, positions_fail):
    settings, db, adapter, request, path = execution
    if positions_fail:
        adapter.positions.side_effect = BrokerError("positions unavailable")
    first = ExecutionGateway(settings, db).submit(adapter, request)
    original = dict(db.query_one("SELECT * FROM orders"))
    reopened = Database(path)
    try:
        second = ExecutionGateway(settings, reopened).submit(adapter, request)
        assert adapter.order_send.call_count == 1
        assert second.order_id == first.order_id
        assert dict(db.query_one("SELECT * FROM orders")) == original
    finally:
        reopened.close()


def test_timeout_is_pending_not_confirmed_rejection(execution):
    settings, db, adapter, request, _ = execution
    result = ExecutionGateway(settings, db).submit(adapter, request)
    assert result.status == OrderState.pending
    assert "not resent" in result.message


def test_direct_gateway_refuses_mismatched_broker(execution):
    settings, db, adapter, request, _ = execution
    with pytest.raises(BrokerError, match="broker identity"):
        ExecutionGateway(settings, db).submit(adapter, request.model_copy(update={"broker_id": "other"}))
    adapter.order_send.assert_not_called()


def test_direct_gateway_blocks_new_key_while_outcome_unknown(execution):
    settings, db, adapter, request, _ = execution
    gateway = ExecutionGateway(settings, db)
    gateway.submit(adapter, request)
    with pytest.raises(BrokerError, match="unresolved"):
        gateway.submit(adapter, request.model_copy(update={"idempotency_key": "new-key"}))
    assert adapter.order_send.call_count == 1


@pytest.mark.parametrize("patch", [{"volume": 0.2}, {"symbol": "EURUSD"},
                                  {"side": "sell"}, {"sl": 1980}, {"broker_id": "other-demo"}])
def test_key_cannot_be_reused_for_different_intent(execution, patch):
    settings, db, adapter, request, _ = execution
    gateway = ExecutionGateway(settings, db)
    gateway.submit(adapter, request)
    if "broker_id" in patch:
        adapter.id = patch["broker_id"]
        adapter.account_info.return_value.broker_id = adapter.id
    with pytest.raises(BrokerError, match="idempotency"):
        gateway.submit(adapter, OrderRequest(**(request.model_dump() | patch)))
    assert adapter.order_send.call_count == 1


@pytest.mark.parametrize("payload", ["{bad", "[]", '{"request": {}}'])
def test_corrupt_existing_record_never_resends(execution, payload):
    settings, db, adapter, request, _ = execution
    db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
               ("old", 1, request.idempotency_key, "pending", adapter.id, request.symbol, payload))
    with pytest.raises(BrokerError, match="idempotency"):
        ExecutionGateway(settings, db).submit(adapter, request)
    adapter.order_send.assert_not_called()
    assert db.query_one("SELECT payload FROM orders")["payload"] == payload


def recovered_position(adapter, **patch):
    return SimpleNamespace(**({"ticket": "ticket-1", "symbol": "XAUUSD", "direction": Direction.up,
                               "volume": 0.1, "entry": 2000, "signal_id": "signal-1",
                               "broker_id": adapter.id} | patch))


@pytest.mark.parametrize("patch", [{"symbol": "EURUSD"}, {"direction": Direction.down},
                                  {"volume": 0.2}, {"broker_id": "other-demo"}])
def test_same_signal_on_different_position_is_not_recovered(execution, patch):
    settings, db, adapter, request, _ = execution
    gateway = ExecutionGateway(settings, db)
    gateway.submit(adapter, request)
    adapter.positions.return_value = [recovered_position(adapter, **patch)]
    result = gateway.submit(adapter, request)
    assert result.status != OrderState.filled
    assert adapter.order_send.call_count == 1


def test_matching_signal_only_is_not_a_verified_broker_order(execution):
    settings, db, adapter, request, _ = execution
    gateway = ExecutionGateway(settings, db)
    first = gateway.submit(adapter, request)
    adapter.positions.return_value = [recovered_position(adapter)]
    result = ExecutionGateway(settings, db).submit(adapter, request)
    assert result.status == OrderState.pending
    assert not result.verified
    assert result.order_id == first.order_id
    assert gateway.unresolved_orders(adapter.id)
    with pytest.raises(BrokerError, match="unresolved"):
        gateway.submit(adapter, request.model_copy(update={"idempotency_key": "new-key"}))
    assert adapter.order_send.call_count == 1


def test_foreign_same_signal_position_before_send_cannot_certify_timeout(execution):
    settings, db, adapter, request, _ = execution
    # The position already existed before No7 attempted this request; equality
    # of its signal metadata is therefore not a unique broker-order link.
    adapter.positions.return_value = [recovered_position(adapter)]
    gateway = ExecutionGateway(settings, db)
    result = gateway.submit(adapter, request)
    assert result.status == OrderState.pending
    assert "broker order/deal identity" in result.message
    assert gateway.unresolved_orders(adapter.id)
    assert adapter.order_send.call_count == 1


def test_broker_filled_then_timeout_remains_unknown_after_restart(execution):
    settings, db, adapter, request, path = execution

    def fill_without_receipt(*args, **kwargs):
        adapter.positions.return_value = [recovered_position(adapter)]
        raise BrokerError("connection lost after order reached server")

    adapter.order_send.side_effect = fill_without_receipt
    first = ExecutionGateway(settings, db).submit(adapter, request)
    reopened = Database(path)
    try:
        after_restart = ExecutionGateway(settings, reopened).submit(adapter, request)
        assert first.status == after_restart.status == OrderState.pending
        assert first.order_id == after_restart.order_id
        assert reopened.query_one("SELECT state FROM orders")["state"] == "pending"
        assert ExecutionGateway(settings, reopened).unresolved_orders(adapter.id)
        assert adapter.order_send.call_count == 1
    finally:
        reopened.close()


def test_two_connections_claim_key_once(execution):
    settings, db, adapter, request, path = execution
    other = Database(path)
    barrier = threading.Barrier(2)
    first, second = ExecutionGateway(settings, db), ExecutionGateway(settings, other)
    # Force both workers to observe absence before either attempts the insert.
    for gateway in (first, second):
        original = gateway._existing

        def initial_read(key, read=original):
            result = read(key)
            if result is None:
                barrier.wait(timeout=5)
            return result

        gateway._existing = initial_read
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda gw: gw.submit(adapter, request), (first, second)))
        assert adapter.order_send.call_count == 1
        assert len({result.order_id for result in results}) == 1
        assert len(db.query("SELECT * FROM orders")) == 1
    finally:
        other.close()


def test_explicit_rejection_is_replayed_without_resend(execution):
    settings, db, adapter, request, _ = execution
    adapter.order_send.side_effect = None
    adapter.order_send.return_value = OrderResult(order_id="adapter-id", idempotency_key="key-1",
                                                 status=OrderState.rejected, message="invalid stops")
    gateway = ExecutionGateway(settings, db)
    first = gateway.submit(adapter, request)
    second = gateway.submit(adapter, request)
    assert first == second
    assert adapter.order_send.call_count == 1
    stored = json.loads(db.query_one("SELECT payload FROM orders")["payload"])
    assert stored["result"]["status"] == "rejected"


def test_crash_after_claim_before_send_stays_reserved(execution):
    settings, db, adapter, request, _ = execution
    db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
               ("claimed", 1, request.idempotency_key, "pending", adapter.id, request.symbol,
                json.dumps({"request": request.model_dump(mode="json"),
                            "account_identity": ExecutionGateway(settings, db)._account(adapter)})))
    result = ExecutionGateway(settings, db).submit(adapter, request)
    assert result.order_id == "claimed"
    assert result.status == OrderState.pending
    adapter.order_send.assert_not_called()


def test_ambiguous_matching_positions_stay_pending(execution):
    settings, db, adapter, request, _ = execution
    gateway = ExecutionGateway(settings, db)
    gateway.submit(adapter, request)
    adapter.positions.return_value = [recovered_position(adapter),
                                      recovered_position(adapter, ticket="ticket-2")]
    assert gateway.submit(adapter, request).status == OrderState.pending
    assert adapter.order_send.call_count == 1


def test_corrupt_result_cannot_be_replayed_or_resent(execution):
    settings, db, adapter, request, _ = execution
    gateway = ExecutionGateway(settings, db)
    gateway.submit(adapter, request)
    row = db.query_one("SELECT * FROM orders")
    payload = json.loads(row["payload"])
    payload["result"]["idempotency_key"] = "unrelated-key"
    db.execute("UPDATE orders SET payload=?", (json.dumps(payload),))
    with pytest.raises(BrokerError, match="idempotency"):
        gateway.submit(adapter, request)
    assert adapter.order_send.call_count == 1


@pytest.mark.parametrize("position_patch,result_patch,duplicate", [
    ({"symbol": "EURUSD"}, {}, False),
    ({"direction": Direction.down}, {}, False),
    ({"broker_id": "other-demo"}, {}, False),
    ({"signal_id": "other-signal"}, {}, False),
    ({"volume": 0.2}, {}, False),
    ({"volume": float("nan")}, {}, False),
    ({"entry": float("inf")}, {}, False),
    ({"entry": 0}, {}, False),
    ({"entry": 2001}, {}, False),
    ({}, {"filled_volume": 0.05}, False),
    ({"volume": 0.05}, {"filled_volume": 0.05}, False),
    ({}, {"filled_volume": float("inf")}, False),
    ({}, {"fill_price": None}, False),
    ({}, {"fill_price": float("nan")}, False),
    ({}, {"ticket": None}, False),
    ({}, {}, True),
])
def test_immediate_fill_requires_matching_evidence(execution, position_patch, result_patch, duplicate):
    settings, db, adapter, request, _ = execution
    adapter.order_send.side_effect = None
    adapter.order_send.return_value = OrderResult(**({
        "order_id": "adapter-id", "idempotency_key": "key-1", "status": OrderState.filled,
        "ticket": "ticket-1", "filled_volume": 0.1, "fill_price": 2000, "verified": True,
    } | result_patch))
    position = recovered_position(adapter, **position_patch)
    adapter.positions.return_value = [position, position] if duplicate else [position]
    gateway = ExecutionGateway(settings, db)
    first = gateway.submit(adapter, request)
    assert not first.verified
    assert not gateway.submit(adapter, request).verified
    assert adapter.order_send.call_count == 1
    stored = json.loads(db.query_one("SELECT payload FROM orders")["payload"])
    assert stored["result"]["verified"] is False


def test_immediate_matching_fill_is_verified(execution):
    settings, db, adapter, request, _ = execution
    adapter.order_send.side_effect = None
    adapter.order_send.return_value = OrderResult(
        order_id="adapter-id", idempotency_key="key-1", status=OrderState.filled,
        ticket="ticket-1", filled_volume=0.1, fill_price=2000,
    )
    adapter.positions.return_value = [recovered_position(adapter)]
    assert ExecutionGateway(settings, db).submit(adapter, request).verified


def test_disabled_fill_verification_cannot_trust_adapter_verified_flag(execution):
    settings, db, adapter, request, _ = execution
    settings.execution["verify_after_send"] = False
    adapter.order_send.side_effect = None
    adapter.order_send.return_value = OrderResult(
        order_id="adapter-id", idempotency_key="key-1", status=OrderState.filled,
        ticket="ticket-1", filled_volume=0.1, fill_price=2000, verified=True,
    )
    gateway = ExecutionGateway(settings, db)
    result = gateway.submit(adapter, request)
    assert result.status == OrderState.filled
    assert not result.verified
    assert "verification disabled" in result.message
    assert gateway.unresolved_orders(adapter.id)
    with pytest.raises(BrokerError, match="unresolved"):
        gateway.submit(adapter, request.model_copy(update={"idempotency_key": "new-key"}))
    adapter.order_send.assert_called_once()


@pytest.mark.parametrize("patch", [{"volume": 0.05}, {"entry": 2001},
                                  {"direction": Direction.down}, {"symbol": "EURUSD"}])
def test_reconcile_reports_changed_position_fields(execution, patch):
    from packages.shared_schemas.models import Position

    settings, db, adapter, _, _ = execution
    position = Position(**(vars(recovered_position(adapter)) |
                           {"open_time": 1, "sl": 1990, "tp": 2020, "current_price": 2000}))
    gateway = ExecutionGateway(settings, db)
    gateway.open_position_snapshot([position])
    adapter.positions.return_value = [position.model_copy(update=patch)]
    status = gateway.reconcile(adapter)
    assert status["ok"] is False
    assert status["changed_at_broker"] == [position.ticket]
    assert status["reconciliation_required"] is True


def test_reconcile_does_not_delete_another_broker_snapshot(execution):
    from packages.shared_schemas.models import Position

    settings, db, adapter, _, _ = execution
    position = Position(**(vars(recovered_position(adapter, broker_id="other-demo")) |
                           {"open_time": 1, "sl": 1990, "tp": 2020, "current_price": 2000}))
    gateway = ExecutionGateway(settings, db)
    gateway.open_position_snapshot([position])
    adapter.positions.return_value = []
    gateway.reconcile(adapter)
    assert db.query_one("SELECT * FROM open_positions WHERE ticket=?", (position.ticket,)) is not None


def test_scoped_snapshot_preserves_other_brokers(execution):
    from packages.shared_schemas.models import Position

    settings, db, adapter, _, _ = execution
    position = Position(**(vars(recovered_position(adapter, broker_id="other-demo")) |
                           {"open_time": 1, "sl": 1990, "tp": 2020, "current_price": 2000}))
    gateway = ExecutionGateway(settings, db)
    gateway.open_position_snapshot([position])
    gateway.open_position_snapshot([], broker_id=adapter.id)
    assert db.query_one("SELECT * FROM open_positions WHERE ticket=?", (position.ticket,)) is not None


def test_snapshot_rejects_cross_account_ticket_collision(execution):
    from packages.shared_schemas.models import Position

    settings, db, adapter, _, _ = execution
    position = Position(**(vars(recovered_position(adapter, broker_id="other-demo")) |
                           {"open_time": 1, "sl": 1990, "tp": 2020, "current_price": 2000}))
    gateway = ExecutionGateway(settings, db)
    gateway.open_position_snapshot([position])
    with pytest.raises(BrokerError, match="collision"):
        gateway.open_position_snapshot([position.model_copy(update={"broker_id": adapter.id})])
    assert json.loads(db.query_one("SELECT payload FROM open_positions")["payload"])["broker_id"] == "other-demo"


def test_unresolved_orders_include_pending_and_unverified_but_not_other_brokers(execution):
    settings, db, adapter, request, _ = execution
    gateway = ExecutionGateway(settings, db)
    gateway.submit(adapter, request)
    assert len(gateway.unresolved_orders(adapter.id)) == 1
    assert gateway.unresolved_orders("other-demo") == []
    row = db.query_one("SELECT * FROM orders")
    result = OrderResult(order_id=row["id"], idempotency_key=request.idempotency_key,
                         status=OrderState.filled, ticket="ticket-1", verified=False)
    gateway._persist_result(row["id"], result, request)
    assert len(gateway.unresolved_orders(adapter.id)) == 1
    result.verified = True
    gateway._persist_result(row["id"], result, request)
    assert gateway.unresolved_orders(adapter.id) == []
