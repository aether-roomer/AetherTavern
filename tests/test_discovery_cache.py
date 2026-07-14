"""Tests for ``server.discovery_cache.DiscoveryCache``.

Exercises the TTL, force, in-flight dedup, exception-drops-entry, and
distinct-key behaviour. Pure asyncio — no FastAPI app needed.
"""
from __future__ import annotations

import asyncio

import pytest

from server import discovery_cache as dc_module
from server.discovery_cache import DiscoveryCache


@pytest.mark.asyncio
async def test_second_call_within_ttl_returns_cached(monkeypatch):
    cache = DiscoveryCache()
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return {"v": calls}

    a = await cache.get_or_fetch(("k",), fetcher)
    b = await cache.get_or_fetch(("k",), fetcher)
    assert a == {"v": 1}
    assert b == {"v": 1}
    assert calls == 1


@pytest.mark.asyncio
async def test_call_after_ttl_refetches(monkeypatch):
    cache = DiscoveryCache()
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return calls

    fake_now = [1000.0]
    monkeypatch.setattr(dc_module.time, "time", lambda: fake_now[0])

    await cache.get_or_fetch(("k",), fetcher)
    fake_now[0] += DiscoveryCache.TTL_SECONDS + 1
    second = await cache.get_or_fetch(("k",), fetcher)
    assert calls == 2
    assert second == 2


@pytest.mark.asyncio
async def test_force_refetches_within_ttl():
    cache = DiscoveryCache()
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_fetch(("k",), fetcher)
    forced = await cache.get_or_fetch(("k",), fetcher, force=True)
    assert calls == 2
    assert forced == 2


@pytest.mark.asyncio
async def test_concurrent_calls_share_inflight_fetch():
    cache = DiscoveryCache()
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return "result"

    t1 = asyncio.create_task(cache.get_or_fetch(("k",), fetcher))
    await started.wait()
    t2 = asyncio.create_task(cache.get_or_fetch(("k",), fetcher))
    # Let t2 enter the cache's decision phase and observe the pending task.
    await asyncio.sleep(0)
    finish.set()
    a, b = await asyncio.gather(t1, t2)
    assert a == "result"
    assert b == "result"
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_force_callers_share_inflight():
    cache = DiscoveryCache()
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return calls

    t1 = asyncio.create_task(cache.get_or_fetch(("k",), fetcher, force=True))
    await started.wait()
    t2 = asyncio.create_task(cache.get_or_fetch(("k",), fetcher, force=True))
    await asyncio.sleep(0)
    finish.set()
    a, b = await asyncio.gather(t1, t2)
    assert a == b
    assert calls == 1


@pytest.mark.asyncio
async def test_fetcher_exception_drops_entry():
    cache = DiscoveryCache()
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("upstream down")
        return "ok"

    with pytest.raises(RuntimeError):
        await cache.get_or_fetch(("k",), fetcher)

    # Next call retries instead of replaying the error.
    value = await cache.get_or_fetch(("k",), fetcher)
    assert value == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_distinct_keys_are_independent():
    cache = DiscoveryCache()
    calls_a = 0
    calls_b = 0

    async def fetch_a():
        nonlocal calls_a
        calls_a += 1
        return "a"

    async def fetch_b():
        nonlocal calls_b
        calls_b += 1
        return "b"

    await cache.get_or_fetch(("a",), fetch_a)
    await cache.get_or_fetch(("b",), fetch_b)
    await cache.get_or_fetch(("a",), fetch_a)  # cached
    await cache.get_or_fetch(("b",), fetch_b)  # cached
    assert calls_a == 1
    assert calls_b == 1
