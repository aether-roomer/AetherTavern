"""Outbound HTTP proxy rules loaded from a YAML file.

Each rule has an optional host glob (default ``"*"``), an optional category
filter (default any), and a proxy URL (or ``null`` for explicit DIRECT).
Rules are matched top-down; first match wins. Unmatched requests fail
with :class:`NoMatchingProxyRule` — there is no implicit DIRECT fallback;
the user opts in by adding ``- proxy: null`` as the last rule.

The same host can route to different proxies in different categories:
each call site asks for the mounts dict (or runs ``lookup``) for its own
category, so category-discrimination happens before httpx mount lookup
ever runs.
"""
from __future__ import annotations

import contextlib
import contextvars
import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlsplit

import httpx
import msgspec.yaml


log = logging.getLogger("aether.proxy_rules")


KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "aetherroom", "tts", "image-import", "image-generation",
        "generic_llm", "tokenizer",
    }
)
KNOWN_PROXY_SCHEMES: frozenset[str] = frozenset(
    {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}
)


class NoMatchingProxyRule(Exception):
    """Raised when an outbound request has no matching rule.

    Surfaced to the user as an SSE ``error`` event (chat / TTS streaming)
    or a per-image failure (import). Names the host + category so the
    user knows which rule to add.
    """

    def __init__(self, host: str, category: str) -> None:
        self.host = host
        self.category = category
        super().__init__(
            f"No proxy rule matched host={host!r} category={category!r}; "
            f"add a matching rule or a `- proxy: null` catch-all."
        )


@dataclass(frozen=True)
class ProxyRule:
    host_pattern: str           # fnmatch glob, lowercased; "*" if missing
    category: Optional[str]     # None = any category
    proxy: Optional[str]        # None = explicit DIRECT


def _validate_proxy(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"`proxy` must be a string or null, got {type(value).__name__}"
        )
    s = value.strip()
    if s.lower() in {"direct", "none", ""}:
        return None
    parts = urlsplit(s)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"malformed proxy URL: {s!r}")
    if parts.scheme.lower() not in KNOWN_PROXY_SCHEMES:
        raise ValueError(
            f"unsupported proxy scheme {parts.scheme!r}; expected one of "
            f"{sorted(KNOWN_PROXY_SCHEMES)}"
        )
    return s


def _validate_category(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"`category` must be a string, got {type(value).__name__}"
        )
    if value not in KNOWN_CATEGORIES:
        raise ValueError(
            f"unknown category {value!r}; expected one of "
            f"{sorted(KNOWN_CATEGORIES)}"
        )
    return value


def _validate_host(value: object) -> str:
    if value is None:
        return "*"
    if not isinstance(value, str):
        raise ValueError(f"`host` must be a string, got {type(value).__name__}")
    s = value.strip().lower()
    return s or "*"


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class _CategoryDispatchTransport(httpx.AsyncBaseTransport):
    """httpx Transport that consults :class:`ProxyRules` per request.

    Picks a proxy by ``rules.lookup(url=..., category=...)``, caches one
    sub-transport per unique proxy URL, and forwards. Raises
    :class:`NoMatchingProxyRule` straight from ``lookup`` when nothing
    matches.

    Wrapping the dispatch in a single mount keeps ``lookup`` and the
    httpx path on identical semantics (top-down, first match wins) and
    sidesteps httpx's automatic URLPattern-specificity sort.
    """

    def __init__(self, rules: "ProxyRules", category: str) -> None:
        self._rules = rules
        self._category = category
        self._sub: dict[Optional[str], httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        proxy = self._rules.lookup(
            url=str(request.url), category=self._category
        )
        t = self._sub.get(proxy)
        if t is None:
            t = (
                httpx.AsyncHTTPTransport(proxy=proxy)
                if proxy
                else httpx.AsyncHTTPTransport()
            )
            self._sub[proxy] = t
        return await t.handle_async_request(request)

    async def aclose(self) -> None:
        for t in self._sub.values():
            await t.aclose()
        self._sub.clear()


class ProxyRules:
    """Loaded, validated, immutable rule list."""

    def __init__(self, rules: list[ProxyRule]) -> None:
        self._rules: tuple[ProxyRule, ...] = tuple(rules)

    @classmethod
    def load(cls, path: Path) -> "ProxyRules":
        data = path.read_bytes()
        try:
            doc = msgspec.yaml.decode(data) if data else {}
        except msgspec.DecodeError as e:
            raise ValueError(f"{path}: invalid YAML: {e}") from e
        if doc is None:
            doc = {}
        if not isinstance(doc, dict):
            raise ValueError(
                f"{path}: top-level must be a mapping with a `rules` key"
            )
        raw_rules = doc.get("rules")
        if raw_rules is None:
            raw_rules = []
        if not isinstance(raw_rules, list):
            raise ValueError(f"{path}: `rules` must be a list")

        rules: list[ProxyRule] = []
        for i, raw in enumerate(raw_rules):
            if not isinstance(raw, dict):
                raise ValueError(f"{path}: rules[{i}] must be a mapping")
            if "proxy" not in raw:
                raise ValueError(
                    f"{path}: rules[{i}] missing required `proxy` field"
                )
            extra = set(raw.keys()) - {"host", "category", "proxy"}
            if extra:
                raise ValueError(
                    f"{path}: rules[{i}] has unknown keys: {sorted(extra)}"
                )
            try:
                rule = ProxyRule(
                    host_pattern=_validate_host(raw.get("host")),
                    category=_validate_category(raw.get("category")),
                    proxy=_validate_proxy(raw["proxy"]),
                )
            except ValueError as e:
                raise ValueError(f"{path}: rules[{i}]: {e}") from e
            rules.append(rule)

        loaded = cls(rules)
        loaded._warn_missing_catchall()
        return loaded

    def _warn_missing_catchall(self) -> None:
        for cat in sorted(KNOWN_CATEGORIES):
            if not self._has_catchall(cat):
                log.warning(
                    "No catch-all proxy rule for category %r — unmatched "
                    "requests in this category will fail. Add `- proxy: null` "
                    "as the last rule to fall back to DIRECT.",
                    cat,
                )

    def _has_catchall(self, category: str) -> bool:
        for r in self._rules:
            if not self._rule_applies_to(r, category):
                continue
            if r.host_pattern == "*":
                return True
        return False

    @staticmethod
    def _rule_applies_to(rule: ProxyRule, category: str) -> bool:
        return rule.category is None or rule.category == category

    def lookup(self, *, url: str, category: str) -> Optional[str]:
        """Return proxy URL or None for explicit DIRECT.

        Raises :class:`NoMatchingProxyRule` when no rule matches.
        """
        host = _host_of(url)
        for r in self._rules:
            if not self._rule_applies_to(r, category):
                continue
            if fnmatch.fnmatchcase(host, r.host_pattern):
                return r.proxy
        raise NoMatchingProxyRule(host, category)

    def httpx_mounts_for(
        self, category: str
    ) -> dict[str, httpx.AsyncBaseTransport]:
        """Mounts dict for ``httpx.AsyncClient(mounts=…)``.

        Always one entry — an ``all://`` mount pointing at a
        :class:`_CategoryDispatchTransport` that does per-request lookup.
        """
        return {"all://": _CategoryDispatchTransport(self, category)}


# --------------------------------------------------------------------------
# Context-scoped current rules
# --------------------------------------------------------------------------
#
# The importers construct ``_ImageSession`` deep inside nested helpers where
# threading a ``proxy_rules`` parameter through every call would be a lot of
# mechanical churn. The route handler sets the contextvar once via
# ``proxy_rules_scope`` and ``_ImageSession`` reads it from there. Contextvars
# propagate naturally across ``await`` and into spawned tasks, so async
# generators inside the same request see the same rules.

_current_rules: contextvars.ContextVar[Optional["ProxyRules"]] = (
    contextvars.ContextVar("aether_current_proxy_rules", default=None)
)


def current_proxy_rules() -> Optional["ProxyRules"]:
    """Return the rules in scope for the current async context."""
    return _current_rules.get()


@contextlib.contextmanager
def proxy_rules_scope(rules: Optional["ProxyRules"]) -> Iterator[None]:
    """Bind ``rules`` as the current proxy rules for this scope.

    Routes that call into the importers wrap the call in this scope so
    that ``_ImageSession`` can look up per-URL proxies without every
    intermediate helper having to forward the rules object.
    """
    token = _current_rules.set(rules)
    try:
        yield
    finally:
        _current_rules.reset(token)


def make_async_client(category: str, **kwargs) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` pre-configured with this category's mounts.

    Reads the rules in scope from :func:`current_proxy_rules`. Routes
    bind that via :func:`proxy_rules_scope` (or via the per-request
    middleware) before calling into any HTTP-emitting code path.

    When no rules are in scope, returns a vanilla ``AsyncClient`` that
    honours environment proxy settings and goes DIRECT otherwise.

    When rules ARE in scope, sets ``trust_env=False`` so a user-set
    ``HTTPS_PROXY`` does not bleed into application traffic — the rules
    file is the only thing that decides where a request goes.
    """
    rules = current_proxy_rules()
    if rules is None:
        return httpx.AsyncClient(**kwargs)
    return httpx.AsyncClient(
        mounts=rules.httpx_mounts_for(category),
        trust_env=False,
        **kwargs,
    )
