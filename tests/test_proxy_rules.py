"""Unit tests for server.proxy_rules — no network."""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import httpx
import pytest

from server.proxy_rules import (
    KNOWN_CATEGORIES,
    NoMatchingProxyRule,
    ProxyRules,
    current_proxy_rules,
    make_async_client,
    proxy_rules_scope,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write_rules(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "proxy.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _load(tmp_path: Path, body: str) -> ProxyRules:
    return ProxyRules.load(_write_rules(tmp_path, body))


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def test_exact_host_match(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "api.example.com"
            proxy: "http://p:8080"
          - proxy: null
    """)
    assert rules.lookup(url="https://api.example.com/v1", category="tts") == "http://p:8080"


def test_subdomain_wildcard(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "*.example.com"
            proxy: "http://p:8080"
          - proxy: null
    """)
    assert rules.lookup(url="https://api.example.com/v1", category="tts") == "http://p:8080"
    assert rules.lookup(url="https://example.com", category="tts") is None  # bare host doesn't match *.foo


def test_catchall_returns_proxy(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - proxy: "http://catchall:3128"
    """)
    assert rules.lookup(url="https://anywhere.example/x", category="aetherroom") == "http://catchall:3128"


def test_null_proxy_means_direct(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "internal.lan"
            proxy: null
          - proxy: "http://default:3128"
    """)
    assert rules.lookup(url="https://internal.lan/x", category="tts") is None
    assert rules.lookup(url="https://elsewhere.lan/x", category="tts") == "http://default:3128"


def test_direct_string_is_synonym_for_null(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "internal.lan"
            proxy: "direct"
          - proxy: null
    """)
    assert rules.lookup(url="https://internal.lan/x", category="tts") is None


def test_case_insensitive_host(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "*.Example.com"
            proxy: "http://p:8080"
          - proxy: null
    """)
    assert rules.lookup(url="https://API.EXAMPLE.com", category="tts") == "http://p:8080"


# --------------------------------------------------------------------------
# Category filter
# --------------------------------------------------------------------------


def test_category_filter_applies_only_to_matching(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - category: tts
            host: "api.shared.com"
            proxy: "socks5://tts-proxy:1080"
          - category: aetherroom
            host: "api.shared.com"
            proxy: "http://corp:8080"
          - proxy: null
    """)
    assert rules.lookup(url="https://api.shared.com/x", category="tts") == "socks5://tts-proxy:1080"
    assert rules.lookup(url="https://api.shared.com/x", category="aetherroom") == "http://corp:8080"


def test_generic_llm_category_routes_independently(tmp_path):
    """Generic LLM traffic gets its own routing slot so users can send AER
    through one proxy and OpenRouter/NanoGPT/etc. through another."""
    rules = _load(tmp_path, """
        rules:
          - category: generic_llm
            host: "openrouter.ai"
            proxy: "http://generic-proxy:9090"
          - category: aetherroom
            host: "*.novelai.net"
            proxy: "http://aer-proxy:8080"
          - proxy: null
    """)
    assert rules.lookup(url="https://openrouter.ai/api/v1/chat/completions", category="generic_llm") == "http://generic-proxy:9090"
    assert rules.lookup(url="https://text.novelai.net/oa/v1/completions", category="aetherroom") == "http://aer-proxy:8080"
    # generic_llm rule does NOT swallow aetherroom traffic to a different host
    assert rules.lookup(url="https://text.novelai.net/oa/v1/completions", category="generic_llm") is None
    assert "generic_llm" in KNOWN_CATEGORIES


def test_missing_category_matches_any(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "any.example"
            proxy: "http://p:8080"
          - proxy: null
    """)
    for cat in KNOWN_CATEGORIES:
        assert rules.lookup(url="https://any.example/x", category=cat) == "http://p:8080"


def test_first_match_wins_across_categories(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "api.example.com"
            proxy: "http://first:1"
          - category: tts
            host: "api.example.com"
            proxy: "http://second:2"
          - proxy: null
    """)
    # First (uncategorised) rule wins, even for tts.
    assert rules.lookup(url="https://api.example.com/x", category="tts") == "http://first:1"


# --------------------------------------------------------------------------
# No-match behaviour
# --------------------------------------------------------------------------


def test_no_match_raises(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - host: "foo.example"
            proxy: "http://p:8080"
    """)
    with pytest.raises(NoMatchingProxyRule) as exc:
        rules.lookup(url="https://bar.example/x", category="tts")
    assert "bar.example" in str(exc.value)
    assert "tts" in str(exc.value)
    assert exc.value.host == "bar.example"
    assert exc.value.category == "tts"


@pytest.mark.asyncio
async def test_httpx_dispatcher_raises_on_no_match(tmp_path):
    """The mounts transport must raise NoMatchingProxyRule when nothing
    matches — proving there's no implicit DIRECT fallback for httpx
    callers either."""
    rules = _load(tmp_path, """
        rules:
          - host: "matched.example"
            proxy: "http://p:8080"
    """)
    mounts = rules.httpx_mounts_for("aetherroom")
    async with httpx.AsyncClient(mounts=mounts, trust_env=False) as client:
        with pytest.raises(NoMatchingProxyRule):
            await client.get("https://unmatched.example/path")


# --------------------------------------------------------------------------
# Startup warning
# --------------------------------------------------------------------------


def test_warns_on_missing_catchall(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="aether.proxy_rules")
    _load(tmp_path, """
        rules:
          - host: "api.example.com"
            proxy: "http://p:8080"
    """)
    msgs = [r.getMessage() for r in caplog.records if r.name == "aether.proxy_rules"]
    # One warning per category, all three categories.
    for cat in KNOWN_CATEGORIES:
        assert any(repr(cat) in m for m in msgs), f"expected warning for {cat!r}"


def test_no_warning_when_catchall_present(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="aether.proxy_rules")
    _load(tmp_path, """
        rules:
          - host: "api.example.com"
            proxy: "http://p:8080"
          - proxy: null
    """)
    msgs = [r.getMessage() for r in caplog.records if r.name == "aether.proxy_rules"]
    assert msgs == []


# --------------------------------------------------------------------------
# Loader validation
# --------------------------------------------------------------------------


def test_rejects_unknown_category(tmp_path):
    with pytest.raises(ValueError, match="unknown category"):
        _load(tmp_path, """
            rules:
              - category: bogus
                proxy: "http://p:8080"
        """)


def test_rejects_malformed_proxy_url(tmp_path):
    with pytest.raises(ValueError, match="malformed proxy URL"):
        _load(tmp_path, """
            rules:
              - proxy: "not-a-url"
        """)


def test_rejects_unsupported_proxy_scheme(tmp_path):
    with pytest.raises(ValueError, match="unsupported proxy scheme"):
        _load(tmp_path, """
            rules:
              - proxy: "ftp://p:21"
        """)


def test_rejects_missing_proxy_field(tmp_path):
    with pytest.raises(ValueError, match="missing required `proxy` field"):
        _load(tmp_path, """
            rules:
              - host: "x.example"
        """)


def test_rejects_unknown_keys(tmp_path):
    with pytest.raises(ValueError, match="unknown keys"):
        _load(tmp_path, """
            rules:
              - host: "x.example"
                proxy: null
                bogus: 1
        """)


def test_rejects_non_list_rules(tmp_path):
    with pytest.raises(ValueError, match="`rules` must be a list"):
        _load(tmp_path, """
            rules: "not a list"
        """)


def test_empty_file_loads_with_zero_rules(tmp_path):
    rules = _load(tmp_path, "")
    # No rules → every lookup raises (no catch-all).
    with pytest.raises(NoMatchingProxyRule):
        rules.lookup(url="https://x.example/", category="tts")


# --------------------------------------------------------------------------
# Context scope + make_async_client
# --------------------------------------------------------------------------


def test_scope_sets_and_resets(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - proxy: null
    """)
    assert current_proxy_rules() is None
    with proxy_rules_scope(rules):
        assert current_proxy_rules() is rules
    assert current_proxy_rules() is None


def test_make_async_client_vanilla_without_rules():
    """No rules in scope → plain AsyncClient with default trust_env."""
    assert current_proxy_rules() is None
    client = make_async_client("aetherroom")
    try:
        assert client.trust_env is True  # httpx default
    finally:
        # Sync close via the underlying transport pool: just rely on GC.
        pass


def test_make_async_client_with_rules_in_scope(tmp_path):
    rules = _load(tmp_path, """
        rules:
          - proxy: null
    """)
    with proxy_rules_scope(rules):
        client = make_async_client("aetherroom")
        try:
            assert client.trust_env is False
            assert client._mounts  # mounts dict is populated
        finally:
            pass


# --------------------------------------------------------------------------
# Dispatcher constructs sub-transports with the right proxy=
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_constructs_correct_sub_transports(tmp_path, monkeypatch):
    """Each unique matched proxy URL should yield exactly one sub-Transport
    constructed with that proxy=; reused for subsequent matching hosts.
    DIRECT rules construct a Transport with no proxy."""
    rules = _load(tmp_path, """
        rules:
          - host: "*.corp.com"
            proxy: "http://corp:8080"
          - host: "*.tunnel.com"
            proxy: "socks5://127.0.0.1:1080"
          - proxy: null
    """)

    constructed: list[object] = []

    class _FakeTransport(httpx.AsyncBaseTransport):
        def __init__(self, *, proxy=None, **kwargs):
            self.proxy = proxy
            constructed.append(self)

        async def handle_async_request(self, request):
            return httpx.Response(200, request=request)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", _FakeTransport)

    mounts = rules.httpx_mounts_for("aetherroom")
    dispatcher = mounts["all://"]

    # Three matched hosts -> three lookups, but only THREE unique proxies
    # (http://corp:8080, socks5://..., None for DIRECT).
    await dispatcher.handle_async_request(httpx.Request("GET", "https://api.corp.com/x"))
    await dispatcher.handle_async_request(httpx.Request("GET", "https://other.corp.com/y"))
    await dispatcher.handle_async_request(httpx.Request("GET", "https://gw.tunnel.com/z"))
    await dispatcher.handle_async_request(httpx.Request("GET", "https://anything.else/q"))

    by_proxy = {t.proxy: t for t in constructed}
    assert set(by_proxy) == {"http://corp:8080", "socks5://127.0.0.1:1080", None}
    # Sub-transport cache: only one Transport per unique proxy URL.
    assert len(constructed) == 3
    # Cache survives across reuse — dispatcher._sub keys mirror by_proxy keys.
    assert set(dispatcher._sub.keys()) == set(by_proxy.keys())

    # aclose propagates to every sub-transport (idempotent).
    await dispatcher.aclose()
    assert dispatcher._sub == {}


# --------------------------------------------------------------------------
# Lifespan auto-detect: <data_dir>/proxy_config.yaml
# --------------------------------------------------------------------------


def test_lifespan_autodetect_default_path(tmp_path, monkeypatch):
    """When AETHER_PROXY_RULES is unset and <data_dir>/proxy_config.yaml
    exists, the lifespan picks it up automatically."""
    from server import main, storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.delenv("AETHER_PROXY_RULES", raising=False)

    # No file -> None.
    assert main._load_proxy_rules() is None

    # File present -> loads.
    (tmp_path / "proxy_config.yaml").write_text(
        "rules:\n  - proxy: null\n", encoding="utf-8"
    )
    rules = main._load_proxy_rules()
    assert rules is not None
    assert rules.lookup(url="https://anything/", category="tts") is None


def test_lifespan_env_var_overrides_autodetect(tmp_path, monkeypatch):
    """AETHER_PROXY_RULES wins over the auto-detected default path."""
    from server import main, storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    # Auto-detect candidate that would route to a fallback proxy.
    (tmp_path / "proxy_config.yaml").write_text(
        "rules:\n  - proxy: 'http://autodetect:8080'\n", encoding="utf-8"
    )
    # Env var pointing at a different file.
    other = tmp_path / "other.yaml"
    other.write_text(
        "rules:\n  - proxy: 'http://envwins:9090'\n", encoding="utf-8"
    )
    monkeypatch.setenv("AETHER_PROXY_RULES", str(other))

    rules = main._load_proxy_rules()
    assert rules is not None
    # The env-var file wins.
    assert rules.lookup(url="https://x/", category="tts") == "http://envwins:9090"


# --------------------------------------------------------------------------
# _ImageSession passes the right proxies= kwarg to curl_cffi per URL
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_session_proxies_per_url(tmp_path, monkeypatch):
    """With a rules file in scope, _ImageSession.fetch must consult
    `lookup(category="image-import")` and pass `proxies={}` to curl_cffi
    on every GET — including DIRECT (None) when the rule says so."""
    import curl_cffi.requests as cc_req

    from server import importers

    rules = _load(tmp_path, """
        rules:
          - host: "blocked.example"
            proxy: "http://blocked-proxy:8080"
          - host: "*.cdn.test"
            proxy: "socks5://socks:1080"
          - proxy: null
    """)

    calls: list[dict] = []

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png", "content-length": "5"}
        content = b"hello"

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, **kwargs):
            calls.append({"url": url, "proxies": kwargs.get("proxies")})
            return _FakeResponse()

    monkeypatch.setattr(cc_req, "AsyncSession", _FakeSession)

    with proxy_rules_scope(rules):
        async with importers._ImageSession() as session:
            await session.fetch("https://blocked.example/img.png")
            await session.fetch("https://media.cdn.test/img.png")
            await session.fetch("https://direct.example/img.png")

    assert calls[0]["proxies"] == {
        "http": "http://blocked-proxy:8080",
        "https": "http://blocked-proxy:8080",
    }
    assert calls[1]["proxies"] == {
        "http": "socks5://socks:1080",
        "https": "socks5://socks:1080",
    }
    # null rule -> DIRECT -> no proxies kwarg.
    assert calls[2]["proxies"] is None


@pytest.mark.asyncio
async def test_image_session_direct_when_no_rules(tmp_path, monkeypatch):
    """Without any rules in scope (the default), _ImageSession.fetch
    passes proxies=None on every request — no env-var fallback, no
    surprise routing."""
    import curl_cffi.requests as cc_req

    from server import importers

    calls: list[dict] = []

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png", "content-length": "5"}
        content = b"hello"

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, **kwargs):
            calls.append({"url": url, "proxies": kwargs.get("proxies")})
            return _FakeResponse()

    monkeypatch.setattr(cc_req, "AsyncSession", _FakeSession)

    assert current_proxy_rules() is None
    async with importers._ImageSession() as session:
        await session.fetch("https://anything.example/img.png")

    assert calls[0]["proxies"] is None
