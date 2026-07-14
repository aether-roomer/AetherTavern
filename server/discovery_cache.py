"""Process-local cache for upstream-discovery results.

Discovery here means "what does an upstream offer?" — model catalogs,
capability probes, cache-config hints. The results are tiny, the same
across callers, and expensive to fetch (one HTTPS round trip per call,
sometimes with retry / proxy resolution on top). One process-wide
cache with a 1 h TTL keyed by arbitrary tuples covers every consumer:
TTS model lists, generic-provider LLM model lists, capability probes,
and provider cache hints all run through the same machinery.

Concurrent callers for the same key share a single in-flight
``asyncio.Task`` — a panicked double-click on a Reload button fires
one upstream request, not two. ``force=True`` joins any in-flight
task too (it's already going upstream — no point starting a parallel
one). A fetcher exception drops the entry entirely so the next call
retries rather than serving a cached error.

Callers must wrap ``get_or_fetch`` in a ``proxy_rules_scope(...)`` so
the first-arriving caller's task captures the proxy rules in context
via ``asyncio.create_task``'s default contextvar propagation.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T | None = None
    fetched_at: float = 0.0
    pending: asyncio.Task[T] | None = None


class DiscoveryCache:
    TTL_SECONDS = 60 * 60

    def __init__(self) -> None:
        self._entries: dict[tuple, _Entry] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(
        self,
        key: tuple,
        fetcher: Callable[[], Awaitable[T]],
        *,
        force: bool = False,
    ) -> T:
        """Return the cached value for ``key`` or fetch a fresh one.

        ``force=True`` bypasses the TTL check but still joins any
        in-flight task for the key. ``force=False`` returns the
        cached value when within TTL and no fetch is pending.
        """
        async with self._lock:
            entry = self._entries.get(key)
            now = time.time()
            if entry is not None and entry.pending is not None:
                pending = entry.pending
            elif (
                not force
                and entry is not None
                and now - entry.fetched_at < self.TTL_SECONDS
            ):
                assert entry.value is not None or entry.fetched_at > 0
                return entry.value  # type: ignore[return-value]
            else:
                pending = asyncio.create_task(fetcher())
                if entry is None:
                    entry = _Entry()
                    self._entries[key] = entry
                entry.pending = pending

        try:
            value = await pending
        except BaseException:
            async with self._lock:
                cur = self._entries.get(key)
                if cur is not None and cur.pending is pending:
                    self._entries.pop(key, None)
            raise

        async with self._lock:
            cur = self._entries.get(key)
            if cur is not None and cur.pending is pending:
                cur.value = value
                cur.fetched_at = time.time()
                cur.pending = None
        return value

    def peek(self, key: tuple):
        """Return the cached value for ``key`` without firing a fetch.

        ``None`` if the entry doesn't exist, is stale, or is still
        in-flight. Used by callers that derive secondary results from a
        primary cache entry (e.g. ``/api/generic/cache-hints`` reads
        the already-cached ``/v1/models`` payload).
        """
        entry = self._entries.get(key)
        if entry is None or entry.pending is not None:
            return None
        if time.time() - entry.fetched_at >= self.TTL_SECONDS:
            return None
        return entry.value
