"""The crawler may not be the easiest way to reach something it would not fetch.

Four boundaries, all of them real, in descending order of how easily they were
reached.

1. `robots_allowed` was an SSRF path

   `discovery._robots_ok` calls it with raw, unfiltered search results, before
   any URL has passed `guard_url` — it runs ahead of the Jev-A screen so a dead
   source costs a robots lookup rather than a judge call. `robots_allowed` never
   validated its target, and `_fetch_robots_text` issued the GET on a pooled
   client that follows redirects. So a search result of `http://169.254.169.254/`
   had `/robots.txt` fetched for it. This is the one hole that needed no
   compromised page, no renderer and no redirect: a search engine result was
   enough.

   Fixed in the function rather than the caller, because the callers are not the
   property that has to hold — the thing that opens the socket is.

2. A robots.txt is a convenient redirector

   Even with the host checked, a *public* host can 302 us anywhere. The pooled
   client follows redirects, so the hop list is the only place the target is
   visible.

3. The browser had no egress policy at all

   Every other path validated its destination. Chromium validated none of them.
   `CrawlerRunConfig` sets no interception, so a rendered page could fetch
   `http://169.254.169.254/latest/meta-data/` as a subresource, or be redirected
   there, from inside the browser — invisible to `guard_url`, invisible to
   `_check_hops`, and invisible to the run's source list, because the browser is
   not the crawler.

4. An open API bound to every interface

   `ALLOW_UNAUTHENTICATED` defaults false and `API_HOST` defaults loopback, so
   this needs three deliberate settings to reach. Together they are an
   unauthenticated, unmetered proxy in front of live Tavily/Gemini/Groq/
   OpenRouter keys *and* of a crawler that fetches any host it is given.

The browser guard is asserted against a fake context that records what was
routed, so it fails if the hook is not installed or if the handler does not
abort — not merely if a flag is set.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.providers.crawl import fetcher  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear():
    """Clear the caches without requiring them to exist.

    Tolerating their absence matters: a fixture that reaches for
    `_EGRESS_CACHE` errors during setup against a tree without it, and an error
    at setup says nothing about which boundary is actually broken.
    """
    caches = [getattr(fetcher, "_robots", None), getattr(fetcher, "_EGRESS_CACHE", None)]
    for c in caches:
        if isinstance(c, dict):
            c.clear()
    yield
    for c in caches:
        if isinstance(c, dict):
            c.clear()


# --- 1. the robots path must validate its own target -------------------------

def test_robots_never_requests_a_private_host(monkeypatch):
    """The reachable one. A search result was enough to reach it.

    `discovery._robots_ok` passes raw search-result URLs straight in, ahead of
    any fetch-time guard, so this is the one boundary with no precondition
    between the attacker and the socket.
    """
    asked: list[tuple[str, str]] = []

    async def _must_not_run(host, scheme):
        asked.append((host, scheme))
        return "User-agent: *\nDisallow: /"

    async def _resolves_private(host):
        return host in ("169.254.169.254", "localhost", "metadata.internal")

    monkeypatch.setattr(fetcher, "_fetch_robots_text", _must_not_run)
    monkeypatch.setattr(fetcher, "_resolve_private", _resolves_private)

    allowed, delay = run(fetcher.robots_allowed("http://169.254.169.254/latest/"))
    assert asked == [], f"a robots request was made to a private host: {asked}"
    assert (allowed, delay) == (True, 0.0)


def test_a_private_host_is_not_reported_as_robots_disallowed(monkeypatch):
    """Blocking here would be a lie recorded as a fact.

    It would mark the source `robots-disallowed` in the run's counters and its
    source row, describing a file nobody read. Allowing lets it reach the
    fetch-time guard, which refuses it with the accurate reason.
    """
    async def _must_not_run(host, scheme):
        raise AssertionError("must not be called")

    async def _resolves_private(host):
        return True

    monkeypatch.setattr(fetcher, "_fetch_robots_text", _must_not_run)
    monkeypatch.setattr(fetcher, "_resolve_private", _resolves_private)
    assert run(fetcher.robots_allowed("http://10.0.0.5/x")) == (True, 0.0)


def test_a_refused_host_is_not_cached(monkeypatch):
    """Otherwise the refusal is remembered from a lookup that never happened.

    Uses a name, not a literal address: a literal private IP is refused by
    `_host_is_private` without a lookup, which is covered separately. This is the
    rebinding shape -- a public name that resolves somewhere private -- and it is
    the one that must be re-checked every time rather than remembered.
    """
    calls = {"n": 0}

    async def _resolves_private(host):
        calls["n"] += 1
        return True

    monkeypatch.setattr(fetcher, "_resolve_private", _resolves_private)
    for _ in range(3):
        run(fetcher.robots_allowed("http://sneaky.example/x"))
    assert calls["n"] == 3, "a refused host was cached, so the check runs once"
    assert not getattr(fetcher, "_robots", {}), \
        "nothing should have been cached for a refusal"


def test_a_public_host_still_gets_its_robots(monkeypatch):
    """The guard must not simply disable robots."""
    asked: list[str] = []

    async def _text(host, scheme):
        asked.append(host)
        return "User-agent: *\nDisallow: /private\n"

    async def _public(host):
        return False

    monkeypatch.setattr(fetcher, "_fetch_robots_text", _text)
    monkeypatch.setattr(fetcher, "_resolve_private", _public)
    allowed, _ = run(fetcher.robots_allowed("https://good.example/public"))
    assert asked == ["good.example"], "the guard disabled robots for a public host"
    assert allowed is True


def test_a_literal_private_ip_is_refused_without_a_dns_lookup(monkeypatch):
    """`127.0.0.1` needs no resolution, so it must not depend on one."""

    async def _must_not_run(host, scheme):
        raise AssertionError("must not be called")

    async def _resolves(host):
        raise AssertionError("no DNS lookup needed for a literal address")

    monkeypatch.setattr(fetcher, "_fetch_robots_text", _must_not_run)
    monkeypatch.setattr(fetcher, "_resolve_private", _resolves)
    assert run(fetcher.robots_allowed("http://127.0.0.1:6379/"))[0] is True


# --- 2. a robots.txt may not redirect us somewhere private --------------------

def test_a_robots_redirect_to_a_private_host_is_refused(monkeypatch):
    """The host was public, which is exactly what makes the redirect the problem."""
    seen: list[str] = []

    class _Resp:
        status_code = 200
        text = "User-agent: *\nDisallow: /"
        history: list = []

        def __init__(self, urls):
            self.history = [type("H", (), {"url": u})() for u in urls]

    async def _get(url, **kw):
        seen.append(url)
        return _Resp(["http://169.254.169.254/latest/meta-data/"])

    class _Client:
        async def get(self, url, **kw):
            return await _get(url, **kw)

    async def _public(host):
        return False

    monkeypatch.setattr(fetcher, "_client_pool", lambda: _Client())
    monkeypatch.setattr(fetcher, "_resolve_private", _public)
    # Now make the hop check see the private redirect.
    monkeypatch.setattr(
        fetcher, "_check_hops",
        _hop_checker(["http://169.254.169.254/latest/meta-data/"]))

    assert run(fetcher.robots_allowed("http://public.example/robots.txt"))[0] is True
    assert seen == ["http://public.example/robots.txt"], seen


def _hop_checker(private_urls):
    """A stand-in for `_check_hops` that raises exactly as the real one does."""
    from app.core.errors import provider_fatal

    async def _check(response):
        for hop in list(getattr(response, "history", []) or []) + [response]:
            url = str(getattr(hop, "url", ""))
            if url in private_urls:
                raise provider_fatal("Fetch refused (redirect to private)")
    return _check


def test_the_robots_fetch_does_check_its_hops():
    """Static: the call has to be in `_fetch_robots_text`.

    Otherwise the test above only proves the stand-in works.
    """
    import inspect

    src = inspect.getsource(fetcher._fetch_robots_text)
    assert "_check_hops" in src, (
        "a robots.txt can 302 anywhere, and the pooled client follows redirects")
    assert "redirect" in src.lower()


# --- 3. the browser must not be an easier way out ----------------------------

class _Route:
    def __init__(self, url):
        self.request = type("R", (), {"url": url})()
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class _Context:
    def __init__(self):
        self.pattern = None
        self.handler = None

    async def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


class _Strategy:
    def __init__(self):
        self.hook = None
        self.bad_hook = False

    def set_hook(self, kind, fn):
        if kind != "on_page_context_created":
            self.bad_hook = True
        self.hook = fn


class _Crawler:
    def __init__(self):
        self.crawler_strategy = _Strategy()


def _install(monkeypatch, private_hosts):
    crawler = _Crawler()

    async def _resolves(host):
        return host in private_hosts

    monkeypatch.setattr(fetcher, "_resolve_private", _resolves)
    fetcher._install_browser_egress(crawler)
    assert crawler.crawler_strategy.hook is not None, (
        "the egress hook was not installed, so the browser is unfiltered")
    ctx = _Context()
    run(crawler.crawler_strategy.hook(context=ctx, page=None))
    assert ctx.pattern == "**/*", (
        "interception must cover every request, not a file extension list")
    return ctx


def test_the_browser_aborts_a_private_subresource(monkeypatch):
    """The subresource case.

    The page itself was guarded before it was ever opened. What it *contains*
    was not: a public page can name an internal host, and Chromium will fetch it
    happily, invisibly, from inside the renderer.
    """
    ctx = _install(monkeypatch, private_hosts=("169.254.169.254",))
    route = _Route("http://169.254.169.254/latest/meta-data/iam/")
    run(ctx.handler(route))
    assert route.aborted is True, "the browser fetched a metadata endpoint"
    assert route.continued is False


def test_the_browser_allows_a_public_subresource(monkeypatch):
    """Otherwise pages break silently, which is its own kind of failure."""
    ctx = _install(monkeypatch, private_hosts=("169.254.169.254",))
    route = _Route("https://cdn.example/app.js")
    run(ctx.handler(route))
    assert route.continued is True
    assert route.aborted is False


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "10.1.2.3", "192.168.0.1", "169.254.169.254",
    "metadata.internal", "foo.local",
])
def test_loopback_private_and_link_local_hosts_are_all_refused(host, monkeypatch):
    """Literal addresses need no DNS, and `_host_is_private` is what catches them."""
    ctx = _install(monkeypatch, private_hosts=())  # resolution says "public"
    route = _Route(f"http://{host}/x")
    run(ctx.handler(route))
    assert route.aborted is True, f"{host} was reachable from the browser"


def test_a_malformed_request_url_is_aborted_rather_than_allowed(monkeypatch):
    """Fail closed. An unparseable URL is not a reason to permit a fetch."""
    ctx = _install(monkeypatch, private_hosts=())
    route = _Route("")
    run(ctx.handler(route))
    assert route.aborted is True


def test_the_egress_verdict_is_cached_per_host(monkeypatch):
    """A page issues hundreds of subresource requests."""
    calls = {"n": 0}

    async def _resolves(host):
        calls["n"] += 1
        return False

    monkeypatch.setattr(fetcher, "_resolve_private", _resolves)
    for _ in range(10):
        run(fetcher._egress_blocked("cdn.example"))
    assert calls["n"] == 1, f"resolved {calls['n']} times for one host"


# --- 4. an open API bound to every interface ---------------------------------

def _settings(**kw):
    base = {"API_KEY": "", "ALLOW_UNAUTHENTICATED": True, "API_HOST": "127.0.0.1"}
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_open_on_loopback_is_allowed():
    """How this has always been run, and it cannot be reached off-box."""
    assert _settings().unsafe_exposure_reason() == ""


def test_no_auth_on_a_public_interface_is_refused():
    reason = _settings(API_HOST="0.0.0.0").unsafe_exposure_reason()
    assert reason, "an unauthenticated API bound to every interface must be refused"
    assert "0.0.0.0" in reason, "the message must name the setting that caused it"
    assert "ALLOW_UNAUTHENTICATED" in reason


def test_a_configured_key_makes_the_opt_in_harmless():
    """Not an open port -- a redundant setting. Refusing it would push people to
    drop the key, which is the opposite of what this check is for."""
    assert _settings(API_KEY="abc", API_HOST="0.0.0.0").unsafe_exposure_reason() == ""


def test_the_defaults_are_safe():
    """The guard is only useful if it does not fire on a fresh checkout."""
    s = Settings(_env_file=None)
    assert s.ALLOW_UNAUTHENTICATED is False
    assert s.unsafe_exposure_reason() == ""


def test_ipv6_all_interfaces_is_also_refused():
    assert _settings(API_HOST="::").unsafe_exposure_reason()


def test_startup_refuses_to_run_the_unsafe_combination():
    """Not just a warning: a log line is not a control."""
    with pytest.raises(RuntimeError) as exc:
        _settings(API_HOST="0.0.0.0").require_safe_exposure()
    assert "refusing to start" in str(exc.value)


def test_startup_allows_the_safe_combinations():
    _settings(API_HOST="127.0.0.1").require_safe_exposure()
    _settings(API_KEY="abc", API_HOST="0.0.0.0").require_safe_exposure()
    Settings(_env_file=None, ALLOW_UNAUTHENTICATED=False,
             API_HOST="0.0.0.0").require_safe_exposure()