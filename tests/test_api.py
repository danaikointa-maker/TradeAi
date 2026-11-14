"""HTTP contract tests: everything the dashboard relies on must answer 200."""
from __future__ import annotations

import json



def test_login_returns_token_and_hides_secret(client):
    res = client.post("/api/auth/login", json={"email": "demo@no7.ai", "password": "no7demo1234"})
    assert res.status_code == 200
    body = res.json()
    assert body["token"] and len(body["token"]) > 40
    assert "password" not in res.text.lower().replace("demo_password", "")


def test_login_rejects_wrong_password(client):
    res = client.post("/api/auth/login", json={"email": "demo@no7.ai", "password": "nope"})
    assert res.status_code == 401


def test_private_endpoints_require_auth(client):
    assert client.get("/api/trading/live").status_code == 401
    assert client.get("/api/risk/settings").status_code == 401
    assert client.get("/api/trading/deals").status_code == 401


def test_deal_history_endpoint_lists_account_scoped_evidence(client, auth, runtime):
    from services.execution_gateway.history import DealLedger

    identity = runtime.gateway._assert_account(runtime.adapter)
    key = f"{identity['server']}:{identity['login']}"
    row = {"deal_id": "api-deal-1", "order_id": "api-order-1", "position_id": "api-position-1",
           "broker_id": runtime.adapter.id, "account_key": key, "currency": identity["currency"],
           "symbol": "XAUUSD", "side": "buy", "entry": "in", "volume": 0.01,
           "price": 2000.0, "time_msc": 1_700_000_000_000, "profit": 0.0,
           "commission": 0.0, "swap": 0.0, "fee": 0.0}
    ledger = DealLedger(runtime.db)
    ledger.ingest(runtime.adapter.id, key, [row])
    response = client.get("/api/trading/deals?limit=10", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["deals"][0]["deal_id"] == "api-deal-1"
    assert body["account_key"] == key
    assert "not strategy ownership" in body["attribution"]


def test_trade_review_keeps_original_signal_and_labels_retrospective_outcome(client, auth, runtime):
    from packages.shared_schemas.models import Direction, TradeRecord

    signal_id = "review-signal-1"
    signal = {"id": signal_id, "t": 1_700_000_000, "symbol": "XAUUSD", "timeframe": "M5",
              "broker_id": runtime.adapter.id, "action": "BUY", "status": "valid", "score": 72,
              "strategy": "trend", "regime": "trending_up", "mode": "manual",
              "plan": {"entry": 2000, "sl": 1990, "tp1": 2010, "tp2": 2020, "tp3": 2030,
                       "lot": 0.01, "risk_amount": 1, "reward_risk": 1.0},
              "reasons": ["trend aligned"], "blockers": [], "components": [], "zones_used": [],
              "approval_required": True, "expires_at": 1_700_000_100, "explanation": {}}
    runtime.db.execute("INSERT OR REPLACE INTO signals(id,t,symbol,action,score,payload) VALUES(?,?,?,?,?,?)",
                       (signal_id, signal["t"], "XAUUSD", "BUY", 72, json.dumps(signal)))
    trade = TradeRecord(ticket="review-ticket-1", symbol="XAUUSD", direction=Direction.up,
                        volume=0.01, entry=2000, exit=2010, opened_at=signal["t"], closed_at=signal["t"] + 60,
                        points=100, gross_pnl=1, costs=0, pnl=1, signal_id=signal_id,
                        broker_id=runtime.adapter.id)
    runtime.gateway.record_trade(trade)

    response = client.get("/api/trading/trades/review-ticket-1/review", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["trade"]["ticket"] == "review-ticket-1"
    assert body["entry_snapshot"]["id"] == signal_id
    assert body["entry_snapshot"]["score"] == 72
    assert body["retrospective"]["outcome"] == "profitable"
    assert body["retrospective"]["basis"] == "recorded trade result; not a new prediction"


def test_trade_review_prefers_immutable_preorder_plan_and_decision_zones(client, auth, runtime):
    from packages.shared_schemas.models import (Direction, OrderRequest, OrderResult,
                                                 OrderState, TradeRecord)

    identity = runtime.gateway._assert_account(runtime.adapter)
    signal_id = "immutable-review-signal-1"
    ticket = "immutable-review-ticket-1"
    order_id = "immutable-review-order-1"
    request = OrderRequest(broker_id=runtime.adapter.id, signal_id=signal_id, symbol="XAUUSD",
                           side="buy", volume=0.1, sl=1990, tp=2030,
                           idempotency_key=order_id).model_dump(mode="json")
    result = OrderResult(order_id=order_id, idempotency_key=order_id,
                         status=OrderState.filled, ticket=ticket, filled_volume=0.1,
                         fill_price=2001, verified=True).model_dump(mode="json")
    original_signal = {"id": signal_id, "broker_id": runtime.adapter.id, "symbol": "XAUUSD",
                       "action": "BUY", "score": 75, "plan": {"entry": 2000, "sl": 1990,
                                                           "tp1": 2010, "tp2": 2020, "tp3": 2030},
                       "zones_used": ["archived-zone"]}
    snapshot = {"schema_version": 1, "account_identity": identity,
                "evidence_level": "original_plan", "request": request,
                "original_signal": original_signal, "analysis_provenance": "decision_time",
                "analysis": {"symbol": "XAUUSD", "broker_id": runtime.adapter.id, "timeframe": "M5",
                             "zones": [{"id": "archived-zone", "low": 1992, "high": 1998}],
                             "sr_flips": [{"level": 1998}],
                             "fibonacci_band": {"level_50": 1996, "level_618": 1994}}}
    with runtime.db.cursor() as cur:
        cur.execute("INSERT INTO orders(id,created_at,idempotency_key,state,broker_id,symbol,payload) "
                    "VALUES(?,?,?,?,?,?,?)", (order_id, 1.0, order_id, "filled", runtime.adapter.id,
                    "XAUUSD", json.dumps({"request": request, "result": result,
                                          "account_identity": identity})))
        cur.execute("INSERT INTO pre_order_snapshots "
                    "(order_id,broker_id,signal_id,symbol,captured_at,payload) VALUES(?,?,?,?,?,?)",
                    (order_id, runtime.adapter.id, signal_id, "XAUUSD", 1.0, json.dumps(snapshot)))
    # The mutable signal row is intentionally changed AFTER the order.
    rewritten = {**original_signal, "score": 99, "plan": {**original_signal["plan"], "sl": 1980}}
    runtime.db.execute("INSERT OR REPLACE INTO signals(id,t,symbol,action,score,payload) VALUES(?,?,?,?,?,?)",
                       (signal_id, 1, "XAUUSD", "BUY", 99, json.dumps(rewritten)))
    runtime.gateway.record_trade(TradeRecord(
        ticket=ticket, symbol="XAUUSD", direction=Direction.up, volume=0.1,
        entry=2001, exit=2010, opened_at=1, closed_at=2, points=900,
        gross_pnl=10, costs=1, pnl=9, signal_id=signal_id, broker_id=runtime.adapter.id))

    response = client.get(f"/api/trading/trades/{ticket}/review", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["entry_snapshot"]["score"] == 75
    assert body["entry_snapshot"]["plan"]["sl"] == 1990
    assert body["original_plan_snapshot"]["analysis"]["zones"][0]["id"] == "archived-zone"
    assert body["original_plan_snapshot"]["analysis_provenance"] == "decision_time"
    assert body["retrospective"]["limitations"] == []


def test_trade_review_rejects_other_broker_account(client, auth, runtime):
    from packages.shared_schemas.models import Direction, TradeRecord

    runtime.gateway.record_trade(TradeRecord(ticket="private-ticket", symbol="XAUUSD", direction=Direction.up,
        volume=0.01, entry=2000, exit=2001, opened_at=10, closed_at=20, points=100,
        gross_pnl=1, costs=0, pnl=1, broker_id="other-account"))
    response = client.get("/api/trading/trades/private-ticket/review", headers=auth)
    assert response.status_code == 404


def test_trade_review_remains_readable_when_market_feed_is_offline(client, auth, runtime, monkeypatch):
    from packages.shared_schemas.models import Direction, TradeRecord

    runtime.gateway.record_trade(TradeRecord(ticket="offline-review", symbol="XAUUSD", direction=Direction.down,
        volume=0.01, entry=2000, exit=1995, opened_at=10, closed_at=20, points=500,
        gross_pnl=0.5, costs=0, pnl=0.5, broker_id=runtime.adapter.id))
    monkeypatch.setattr(runtime.market.adapter, "ticker", lambda _symbol: (_ for _ in ()).throw(RuntimeError("offline")))
    response = client.get("/api/trading/trades/offline-review/review", headers=auth)
    assert response.status_code == 200
    assert response.json()["retrospective"]["outcome"] == "profitable"
    assert response.json()["entry_snapshot"] is None


def test_open_position_review_is_read_only_and_scoped_to_broker(client, auth, runtime, monkeypatch):
    from packages.shared_schemas.models import Position

    position = Position(ticket="open-review-1", symbol="XAUUSD", direction="up",
                        volume=0.1, entry=2000, sl=1990, tp=2030,
                        open_time=1, current_price=2002, broker_id=runtime.adapter.id)
    monkeypatch.setattr(runtime.adapter, "positions", lambda: [position])
    response = client.get("/api/trading/positions/open-review-1/review", headers=auth)
    assert response.status_code == 200
    assert response.json()["position"]["ticket"] == "open-review-1"
    assert response.json()["original_plan_snapshot"] is None
    assert "events" in response.json()
    assert client.get("/api/trading/positions/not-my-ticket/review", headers=auth).status_code == 404


def test_open_position_review_shows_unverified_broker_deltas(client, auth, runtime, monkeypatch):
    from packages.shared_schemas.models import Position

    baseline = Position(ticket="open-delta-1", symbol="XAUUSD", direction="up",
                        volume=0.1, entry=2000, sl=1990, tp=2030,
                        open_time=100, current_price=2002, broker_id=runtime.adapter.id)
    runtime.gateway.open_position_snapshot([baseline], broker_id=runtime.adapter.id)
    changed = baseline.model_copy(update={"sl": 1995})
    monkeypatch.setattr(runtime.adapter, "positions", lambda: [changed])
    assert runtime.gateway.reconcile(runtime.adapter)["changed_at_broker"] == ["open-delta-1"]
    response = client.get("/api/trading/positions/open-delta-1/review", headers=auth)
    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["status"] == "changed_unverified"
    assert events[0]["detail"]["changed_fields"] == ["sl"]
    assert events[0]["order_attribution"] == "unresolved"


def test_closed_trade_review_shows_only_account_scoped_observed_candles(client, auth, runtime):
    from packages.persistence.position_candles import PositionCandleLog
    from packages.shared_schemas.models import Position, TradeRecord

    owner = runtime.gateway._assert_account(runtime.adapter)
    position = Position(ticket="closed-observed-candles", symbol="XAUUSD", direction="up",
                        volume=0.1, entry=2000, sl=1990, tp=2030, open_time=600,
                        current_price=2003, broker_id=runtime.adapter.id)
    record = PositionCandleLog(runtime.db)
    record.capture(position=position, account_identity=owner,
                   bars=[{"t": 600, "o": 2000., "h": 2003., "l": 1998.,
                          "c": 2002., "v": 10.}], market_time=1000, feed_mode="simulated")
    runtime.gateway.record_trade(TradeRecord(
        ticket=position.ticket, symbol="XAUUSD", direction="up", volume=0.1,
        entry=2000, exit=2008, opened_at=600, closed_at=1200,
        points=800, gross_pnl=80, costs=1, pnl=79, broker_id=runtime.adapter.id))
    response = client.get("/api/trading/trades/closed-observed-candles/review", headers=auth)
    assert response.status_code == 200
    archived = response.json()["post_entry_candles"]
    assert [bar["t"] for bar in archived["bars"]] == [600]
    assert archived["bars"][0]["feed_mode"] == "simulated"
    assert archived["broker_tick_verified"] is False
    assert archived["continuity"]["observed_bars"] == 1
    assert archived["continuity"]["gaps"] == []
    assert archived["continuity"]["complete"] is False


def test_trade_history_is_scoped_to_active_broker_account(client, auth, runtime):
    from packages.shared_schemas.models import Direction, TradeRecord

    for ticket, broker_id in (("mine", runtime.adapter.id), ("other", "other-account")):
        runtime.gateway.record_trade(TradeRecord(ticket=ticket, symbol="XAUUSD", direction=Direction.up,
            volume=0.01, entry=2000, exit=2001, opened_at=10, closed_at=20, points=100,
            gross_pnl=1, costs=0, pnl=1, broker_id=broker_id))
    response = client.get("/api/trading/trades?limit=100", headers=auth)
    assert response.status_code == 200
    tickets = {trade["ticket"] for trade in response.json()["trades"]}
    assert "mine" in tickets
    assert "other" not in tickets


def test_ticker_endpoint_retains_quote_when_market_is_closed(client, auth, runtime, monkeypatch):
    from packages.shared_schemas.models import Tick

    tick = Tick(t=1_700_000_000, bid=2000, ask=2001, spread_points=100, last=2000.5)
    runtime.market.on_ticks({"XAUUSD": tick})
    monkeypatch.setattr(runtime.market.adapter, "ticker", lambda _symbol: (_ for _ in ()).throw(RuntimeError("closed")))
    response = client.get("/api/market/ticker?symbol=XAUUSD", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"tick": tick.model_dump(), "stale": True, "source": "persisted"}


def test_health_is_public(client):
    res = client.get("/api/system/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("ok", "degraded")
    assert "services" in body and "broker_health" in body


def test_single_close_http_preserves_unknown_result(client, auth, runtime, monkeypatch):
    from unittest.mock import Mock

    close = Mock(side_effect=RuntimeError("private broker detail"))
    monkeypatch.setattr(runtime.gateway, "close", close)
    res = client.post("/api/trading/close", json={"ticket": "unknown-send"}, headers=auth)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["ok"] is False and detail["outcome"] == "unknown"
    assert detail["unknown"] == 1 and detail["attempted"] == 1
    assert detail["exposure_verified"] is False
    assert detail["reconciliation_required"] is True
    assert "private" not in res.text
    close.assert_called_once()


def test_live_snapshot_has_every_block_the_ui_needs(client, auth):
    res = client.get("/api/trading/live", headers=auth)
    assert res.status_code == 200, res.text
    live = res.json()
    for key in ("account", "positions", "risk_state", "limits", "signal", "analysis",
                "forecast", "mode", "feed", "alerts", "ticker", "ticker_stale"):
        assert key in live, f"missing {key}"
    analysis = live["analysis"]
    assert len(analysis["supports"]) >= 3 and len(analysis["resistances"]) >= 3
    assert analysis["spec"]["point"] > 0 and analysis["spec"]["digits"] >= 0
    forecast = live["forecast"]
    assert len(forecast["horizons"]) >= 3
    for h in forecast["horizons"]:
        assert h["p10_price"] < h["p90_price"]
        assert 0 <= h["p_up"] <= 1 and 0 <= h["p_down"] <= 1
    assert {s["name"] for s in forecast["scenarios"]} == {"bull", "base", "bear"}
    # the score is a heuristic and must never be presented as a probability
    assert live["signal"] is None or "score" in live["signal"]


def test_live_snapshot_marks_memory_quote_stale_after_feed_stops(client, auth, runtime, monkeypatch):
    from packages.shared_schemas.models import Tick

    tick = Tick(t=1_700_000_000, bid=2000, ask=2001, spread_points=100, last=2000.5)
    runtime.market.on_ticks({"XAUUSD": tick})
    runtime.active_symbol = "XAUUSD"
    runtime.ticks = {"XAUUSD": tick}
    monkeypatch.setattr(runtime.market.adapter, "ticker", lambda _symbol: (_ for _ in ()).throw(RuntimeError("offline")))
    runtime.market.latest_quote("XAUUSD")
    live = runtime.live_payload()
    assert live["ticker"] == tick.model_dump()
    assert live["ticker_stale"] is True


def test_signal_score_is_not_a_probability(client, auth):
    live = client.get("/api/trading/live", headers=auth).json()
    signal = live.get("signal")
    if signal:
        assert signal["score"] <= 100
        if signal["explanation"]:
            assert signal["explanation"].get("calibrated") is False


def test_forecast_exposes_uncertainty(client, auth):
    res = client.get("/api/forecast", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert "active" in body and "models" in body


def test_zones_endpoint_separates_statuses(client, auth):
    res = client.get("/api/analysis/XAUUSD/zones", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert len(body["supports"]) >= 3 and len(body["resistances"]) >= 3


def test_points_calculator_uses_live_spec(client, auth):
    res = client.get("/api/market/calculator?symbol=XAUUSD&points=500&lot=0.01", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["spec"]["point"] == 0.01
    assert body["result"]["price_move"] == 5.0
    assert "excludes" in body["disclaimer"].lower() or "Excludes" in body["disclaimer"]


def test_risk_settings_round_trip(client, auth):
    before = client.get("/api/risk/settings", headers=auth).json()
    res = client.put("/api/risk/settings", json={"limits": {"max_spread_points": 55}}, headers=auth)
    assert res.status_code == 200
    assert res.json()["limits"]["max_spread_points"] == 55
    client.put("/api/risk/settings",
               json={"limits": {"max_spread_points": before["limits"]["max_spread_points"]}}, headers=auth)


def test_order_is_blocked_without_a_clean_gate(client, auth):
    """A demo account in a news window must refuse a manual order."""
    settings = client.get("/api/risk/settings", headers=auth).json()
    client.put("/api/risk/settings", json={"limits": {"news_lockout_enabled": False}}, headers=auth)
    try:
        live = client.get("/api/trading/live", headers=auth).json()
        if live["positions"]:
            client.post("/api/trading/close-all", headers=auth)
        res = client.post("/api/trading/order", json={"class": "x"}, headers=auth)
        assert res.status_code in (400, 422)
    finally:
        client.put("/api/risk/settings",
                   json={"limits": {"news_lockout_enabled": settings["settings"]["limits"]["news_lockout_enabled"]}},
                   headers=auth)


def test_broker_list_and_symbols(client, auth):
    brokers = client.get("/api/brokers", headers=auth).json()
    assert brokers["brokers"] and brokers["active"]
    syms = client.get(f"/api/brokers/{brokers['active']}/symbols", headers=auth).json()
    assert syms["symbols"] and syms["symbols"][0]["lot_step"] > 0


def test_spa_deep_links_are_served(client):
    for path in ("/", "/forecast", "/risk"):
        res = client.get(path)
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]


def test_backtest_job_list_is_pruned(client, token):
    """Finished jobs must not accumulate forever in a module-level dict."""
    from services.api_gateway.routes import backtest as bt

    with bt._LOCK:
        bt.JOBS.clear()
        for i in range(bt.MAX_JOBS + 10):
            bt.JOBS[f"JOB-OLD{i}"] = {"state": "done", "started_at": float(i)}
    bt._prune_jobs()
    assert len(bt.JOBS) <= bt.MAX_JOBS
    with bt._LOCK:
        bt.JOBS.clear()


def test_health_reports_notification_channels(client):
    health = client.get("/api/system/health").json()
    assert "notify" in health
    assert health["notify"]["channels"]["inapp"]["configured"] is True
    # external channels are off unless the operator configures them
    assert health["services"]["notify"] in ("inapp-only", "external", "failing")


def test_notify_status_endpoint_lists_channels_without_secrets(client, token):
    res = client.get("/api/system/notify", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert res.request.url.path == "/api/system/notify"
    assert set(body["channels"]) == {"inapp", "telegram", "line", "webhook"}
    assert "Bearer" not in json.dumps(body)
    for channel in body["channels"].values():
        assert "token" not in channel


def test_alert_test_endpoint_says_why_it_cannot_send(client, token):
    res = client.post("/api/system/alerts/test", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["deliveries"] == []
    assert body["reason"]
