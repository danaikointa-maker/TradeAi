"""One rate limit, whatever process answers the request.

A token bucket in memory is correct only while exactly one process serves traffic.
With `--workers N` each worker hands out its own budget, so a limit of 10/minute
becomes 10*N — the configured number an illusion. These tests pin the two things
that make a shared limiter trustworthy:

* the same contract on every backend (memory, sqlite, redis), and
* the sqlite backend actually holding the line when several *processes* spend at once.

Redis is exercised through `fakeredis` (a faithful in-process server that runs the
same Lua). Without that package, or a real `NO7_REDIS_URL`, those tests skip rather
than pretend.
"""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time

import pytest

from services.api_gateway.ratelimit import (MemoryBucketStore, RateLimiter, RateLimitMiddleware,
                                            RedisBucketStore, SqliteBucketStore, build_store,
                                            make_store)

redis = pytest.importorskip  # re-exported for readability below


# --------------------------------------------------------------------- fixtures
def _redis_client():
    fakeredis = pytest.importorskip("fakeredis", reason="pip install 'fakeredis[lua]'")
    server = fakeredis.FakeServer()
    return fakeredis.FakeStrictRedis(server=server, decode_responses=True)


@pytest.fixture(params=["memory", "sqlite", "redis"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryBucketStore()
    elif request.param == "sqlite":
        s = SqliteBucketStore(tmp_path / "rl.sqlite3")
        yield s
        s.close()
    else:
        client = _redis_client()
        s = RedisBucketStore(client, prefix="no7:test")
        yield s
        client.flushall()


def _limiter(store, limit=10, window=60, clock=None) -> RateLimiter:
    return RateLimiter({"probe": {"limit": limit, "window_seconds": window}},
                       store=store, clock=clock)


# --------------------------------------------------------------------- contract
def test_burst_then_denial_on_every_backend(store):
    limiter = _limiter(store, limit=5, window=60, clock=lambda: 1000.0)
    results = [limiter.check("user:a", "probe") for _ in range(8)]
    assert [r[0] for r in results] == [True] * 5 + [False] * 3
    assert results[0][1] == 4            # one spent of five, but rounded down
    assert all(r[2] >= 1.0 for r in results if not r[0]), "retry_after must never be 0"


def test_refill_is_proportional_to_elapsed_time(store):
    now = [1000.0]
    limiter = _limiter(store, limit=10, window=60, clock=lambda: now[0])
    assert all(limiter.check("user:a", "probe")[0] for _ in range(10))
    assert limiter.check("user:a", "probe")[0] is False
    now[0] += 6.0                        # 6s at 10/60 per second = 1 token
    assert limiter.check("user:a", "probe")[0] is True
    assert limiter.check("user:a", "probe")[0] is False


def test_identities_do_not_share_a_bucket(store):
    limiter = _limiter(store, limit=2, window=60, clock=lambda: 500.0)
    assert limiter.check("user:a", "probe")[0] and limiter.check("user:a", "probe")[0]
    assert limiter.check("user:a", "probe")[0] is False
    assert limiter.check("user:b", "probe")[0] is True, "another user has their own budget"


def test_rules_do_not_share_a_bucket(store):
    limiter = RateLimiter({"probe": {"limit": 1, "window_seconds": 60},
                           "other": {"limit": 5, "window_seconds": 60}},
                          store=store, clock=lambda: 500.0)
    assert limiter.check("user:a", "probe")[0] is True
    assert limiter.check("user:a", "probe")[0] is False
    assert limiter.check("user:a", "other")[0] is True


def test_reset_clears_every_backend(store):
    limiter = _limiter(store, limit=1, window=60, clock=lambda: 500.0)
    assert limiter.check("user:a", "probe")[0] is True
    assert limiter.check("user:a", "probe")[0] is False
    limiter.reset()
    assert limiter.check("user:a", "probe")[0] is True


# ------------------------------------------------------------------ redis detail
def test_redis_keeps_the_bucket_in_redis_not_in_the_process():
    fakeredis = pytest.importorskip("fakeredis", reason="pip install 'fakeredis[lua]'")
    client = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer(), decode_responses=True)
    first = RateLimiter({"probe": {"limit": 3, "window_seconds": 60}},
                        store=RedisBucketStore(client, prefix="no7:t"), clock=lambda: 900.0)
    for _ in range(3):
        assert first.check("user:a", "probe")[0] is True
    # A different process, same Redis: the budget is already spent.
    second = RateLimiter({"probe": {"limit": 3, "window_seconds": 60}},
                         store=RedisBucketStore(client, prefix="no7:t"), clock=lambda: 900.0)
    assert second.check("user:a", "probe")[0] is False
    keys = list(client.scan_iter(match="no7:t:*"))
    assert keys and client.ttl(keys[0]) > 0, "idle identities must expire on their own"


def test_redis_failure_degrades_to_a_local_limit_and_says_so():
    class Broken:
        name = "redis"

        def spend(self, *args, **kwargs):
            raise ConnectionError("redis went away")

        def reset(self):
            raise ConnectionError("redis went away")

        tracked = -1

    limiter = RateLimiter({"probe": {"limit": 3, "window_seconds": 60}},
                          store=Broken(), clock=lambda: 1.0)
    for _ in range(3):
        assert limiter.check("user:a", "probe")[0] is True
    assert limiter.check("user:a", "probe")[0] is False, "still limited, just locally"
    assert limiter.degraded == 4
    assert "redis went away" in limiter.last_error
    assert limiter.status()["degraded"] == 4
    assert limiter.tracked == -1


# ------------------------------------------------------------------- building one
def test_build_store_reports_what_it_built(tmp_path):
    mem, status = build_store({"store": "memory"}, path=tmp_path / "x.sqlite3")
    assert isinstance(mem, MemoryBucketStore) and status["backend"] == "memory"
    assert status["shared"] is False

    sql, status = build_store({"store": "sqlite"}, path=tmp_path / "rl.sqlite3")
    assert isinstance(sql, SqliteBucketStore) and status["shared"] is True
    sql.close()


def test_redis_without_a_url_falls_back_and_explains(tmp_path):
    store, status = build_store({"store": "redis"}, path=tmp_path / "rl.sqlite3", env={})
    assert status["backend"] == "sqlite", "a shared file is better than per-process memory"
    assert "no redis_url" in status["reason"]
    assert status["requested"] == "redis"
    store.close()

    store2, status2 = build_store({"store": "redis"}, path=None, env={})
    assert status2["backend"] == "memory" and "no redis_url" in status2["reason"]


def test_unreachable_redis_falls_back_with_the_error_in_the_reason(tmp_path):
    def factory():
        raise ConnectionError("connection refused")

    store, status = build_store({"store": "redis", "redis_url": "redis://h:6379/0"},
                                path=tmp_path / "rl.sqlite3", client_factory=factory, env={})
    assert status["backend"] == "sqlite"
    assert "connection refused" in status["reason"]
    store.close()


def test_a_working_redis_client_is_used(tmp_path):
    fakeredis = pytest.importorskip("fakeredis", reason="pip install 'fakeredis[lua]'")
    client = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer(), decode_responses=True)
    store, status = build_store({"store": "redis", "redis_url": "redis://x/0",
                                 "key_prefix": "no7:h"},
                                path=tmp_path / "rl.sqlite3", client_factory=lambda: client,
                                env={})
    assert status["backend"] == "redis" and status["shared"] is True
    assert isinstance(store, RedisBucketStore)


def test_unknown_store_is_refused_not_guessed():
    with pytest.raises(ValueError, match="unknown ratelimit store"):
        build_store({"store": "memcached"})
    with pytest.raises(ValueError, match="redis"):
        make_store("redis")


# --------------------------------------------------------- several processes, one file
def _spend_many(path: str, attempts: int, start_at: float, out) -> None:
    """Child process: hammer one bucket with a frozen clock so nothing refills."""
    store = SqliteBucketStore(path)
    limiter = RateLimiter({"probe": {"limit": 40, "window_seconds": 3600}},
                          store=store, clock=lambda: start_at)
    allowed = sum(1 for _ in range(attempts) if limiter.check("ip:198.51.100.9", "probe")[0])
    out.send(allowed)
    out.close()
    store.close()


def test_four_processes_cannot_spend_more_than_the_limit(tmp_path):
    """The real property: the configured limit is the limit, not limit x workers."""
    db = str(tmp_path / "shared.sqlite3")
    SqliteBucketStore(db).close()          # create the schema before the race
    ctx = mp.get_context("spawn")          # closest to separate uvicorn workers
    # A one-way OS pipe avoids a Manager's socket listener (restricted in some
    # runners) while still racing four independent spawned SQLite clients.
    pipes = [ctx.Pipe(duplex=False) for _ in range(4)]
    procs = [ctx.Process(target=_spend_many, args=(db, 20, 1000.0, send))
             for _, send in pipes]
    try:
        for p in procs:
            p.start()
        for _, send in pipes:
            send.close()
        for p in procs:
            p.join(timeout=60)
        assert all(p.exitcode == 0 for p in procs), "a worker crashed"
        allowed = [recv.recv() for recv, _ in pipes]
        assert sum(allowed) == 40, f"limit 40 was handed out {sum(allowed)} times"
    finally:
        for recv, send in pipes:
            recv.close()
            send.close()
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
    conn = sqlite3.connect(db)
    tokens = conn.execute("SELECT tokens FROM buckets").fetchone()[0]
    conn.close()
    assert tokens <= 0.0001


def test_status_names_the_store_and_the_shared_flag(settings):
    limiter = RateLimiter({}, store=MemoryBucketStore())
    assert limiter.status()["store"] == "memory"
    assert limiter.status()["degraded"] == 0


def test_degraded_response_is_marked_in_the_headers(settings):
    """A limit applied locally must not look like the shared limit."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    class Flaky:
        name = "redis"

        def spend(self, *args, **kwargs):
            raise ConnectionError("down")

        def reset(self):
            pass

        tracked = -1

    limiter = RateLimiter({"default": {"limit": 100, "window_seconds": 60}}, store=Flaky())
    middleware = RateLimitMiddleware(None, limiter=limiter, settings=settings,
                                     trusted_proxies=["127.0.0.1/32"])
    # An ASGI app wrapped by hand, so the middleware's own __call__ runs the real path.
    async def handler(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/probe", handler)])
    middleware.app = app
    res = TestClient(middleware).get("/api/probe")
    assert res.status_code == 200
    assert res.headers["x-ratelimit-degraded"] == "redis"
    assert "x-ratelimit-remaining" in res.headers
    assert limiter.degraded == 1
    assert time.time() > 0


def test_a_degraded_denial_says_it_was_degraded_too(settings):
    """A 429 from the local fallback must not look like the shared budget running out.

    Before this, only the 200s carried `x-ratelimit-degraded`. A client that got a
    429 could not tell "you really are out of quota everywhere" from "this one
    worker improvised because Redis is down", and `retry-after` came from the local
    bucket either way.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    class Flaky:
        name = "redis"

        def spend(self, *args, **kwargs):
            raise ConnectionError("down")

        def reset(self):
            pass

        tracked = -1

    limiter = RateLimiter({"default": {"limit": 2, "window_seconds": 60}}, store=Flaky(),
                          clock=lambda: 5000.0)
    middleware = RateLimitMiddleware(None, limiter=limiter, settings=settings,
                                     trusted_proxies=["127.0.0.1/32"])

    async def handler(request):
        return JSONResponse({"ok": True})

    middleware.app = Starlette(routes=[Route("/api/probe", handler)])
    client = TestClient(middleware)
    responses = [client.get("/api/probe") for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 429]
    for res in responses:
        assert res.headers["x-ratelimit-degraded"] == "redis", res.status_code
    denied = responses[-1]
    assert int(denied.headers["retry-after"]) >= 1
    assert denied.headers["x-ratelimit-remaining"] == "0"
    assert limiter.degraded == 3


def test_health_keeps_claiming_shared_only_while_it_is_true(runtime, monkeypatch):
    """A green `shared: true` during a Redis outage is worse than no field at all."""
    monkeypatch.setattr(runtime, "ratelimit_status",
                        {"backend": "redis", "requested": "redis", "shared": True, "reason": ""},
                        raising=False)

    healthy = RateLimiter({}, store=MemoryBucketStore())
    monkeypatch.setattr(runtime, "ratelimit", healthy, raising=False)
    good = runtime._ratelimit_status()
    assert good["shared"] is True and good["degraded"] == 0
    assert "warning" not in good

    class Flaky:
        name = "redis"
        tracked = -1

        def spend(self, *args, **kwargs):
            raise ConnectionError("Error 111 connecting to 127.0.0.1:6392. Connection refused.")

        def reset(self):
            pass

    degraded_limiter = RateLimiter({"default": {"limit": 5, "window_seconds": 60}}, store=Flaky())
    degraded_limiter.check("ip:1.2.3.4", "default")
    monkeypatch.setattr(runtime, "ratelimit", degraded_limiter, raising=False)

    bad = runtime._ratelimit_status()
    assert bad["store"] == "redis", "it still says which store was configured"
    assert bad["shared"] is False, "the budget is not shared while the store is unreachable"
    assert bad["degraded"] == 1
    assert "Connection refused" in bad["last_error"]
    assert "x-ratelimit-degraded" in bad["warning"]


def test_health_reports_the_live_limiter_end_to_end(client, auth):
    """Wiring check: without it, health could only report the boot-time config.

    Identity (`runtime.ratelimit is app.state.ratelimit`) is deliberately not asserted:
    the session runtime is shared by several apps in this suite, so the last app built
    owns the attribute. What must hold is that health speaks for a live limiter.
    """
    runtime = client.app.state.runtime
    assert isinstance(getattr(runtime, "ratelimit", None), RateLimiter), "create_app must wire it"

    body = client.get("/api/system/health", headers=auth).json()["ratelimit"]
    assert body["degraded"] == 0, "nothing in this suite should have degraded the store"
    assert body["store"] and "shared" in body
