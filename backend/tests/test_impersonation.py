"""Phase-2 impersonation tests — allowlist gating, waterfall order, audit, SSRF.

Hermetic: no network. curl_cffi, robots, throttle, and DNS guards are faked or
pointed at unroutable/local targets. The allowlist is monkeypatched per test;
a missing/empty allowlist must ALWAYS mean the rung never fires.
"""
import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import impersonation as impersonation_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "IMPERSO_ALLOWLIST",
                        "example.com, sub.test.org")
    monkeypatch.setattr(config_mod.settings, "IMPERSO_IMPERSONATE", "chrome")
    return config_mod.settings


@pytest.fixture
def unlisted(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "IMPERSO_ALLOWLIST", "")
    return config_mod.settings


# -- allowlist matching --------------------------------------------------------
def test_allowlist_exact_suffix_case(listed):
    assert impersonation_svc.is_allowlisted("https://example.com/a")
    assert impersonation_svc.is_allowlisted("https://WWW.EXAMPLE.COM/a")
    assert impersonation_svc.is_allowlisted("https://deep.sub.test.org/x")
    assert impersonation_svc.is_allowlisted("https://example.com./a")  # trailing dot
    assert not impersonation_svc.is_allowlisted("https://notexample.com/a")
    assert not impersonation_svc.is_allowlisted("https://example.com.evil.org/a")
    assert not impersonation_svc.is_allowlisted("https://other.org/a")
    assert not impersonation_svc.is_allowlisted("not a url")
    assert not impersonation_svc.is_allowlisted("")


def test_empty_allowlist_never_matches(unlisted):
    assert impersonation_svc.allowlist() == []
    assert not impersonation_svc.is_allowlisted("https://example.com/a")


# -- waterfall ordering ----------------------------------------------------------
def test_methods_for_splices_rung_only_when_listed(listed):
    assert crawler_svc._methods_for("web:http", "https://example.com/a") == (
        "http", "impersonate", "crawl4ai")
    assert crawler_svc._methods_for("web:crawl4ai", "https://example.com/a") == (
        "crawl4ai", "http", "impersonate")
    assert crawler_svc._methods_for("structured:api", "https://example.com/a") == (
        "http", "impersonate")  # http-bearing routes splice when listed
    assert crawler_svc._methods_for("web:http", "https://other.org/a") == (
        "http", "crawl4ai")  # off-list: exact MVP order


def test_one_falls_to_impersonate_when_listed(listed):
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            raise AppError("E_PROVIDER_TRANSIENT", "timeout", 502)
        if method == "impersonate":
            return {"url": url, "html": "<p>x</p>", "method": "impersonate"}
        raise AssertionError(f"should not reach {method}")

    page = run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "impersonate"
    assert calls[0] == "http" and "impersonate" in calls
    assert "crawl4ai" not in calls  # impersonate succeeded; no further fallback


def test_one_never_impersonates_when_unlisted(unlisted):
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            raise AppError("E_PROVIDER_TRANSIENT", "timeout", 502)
        return {"url": url, "html": "<p>x</p>", "method": method}

    page = run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert "impersonate" not in calls
    assert page["method"] == "crawl4ai"


# -- refusal + SSRF (no network) ---------------------------------------------------
def test_offlist_refusal_is_fatal_before_network(unlisted):
    with pytest.raises(AppError) as ei:
        run(impersonation_svc.impersonate_fetch("https://example.com/a"))
    assert ei.value.code == "E_PROVIDER_FATAL"


def test_allowlisted_private_host_refused(monkeypatch):
    # localhost ON the allowlist: must still be denied by the SSRF guard
    # (local resolution only — no external traffic).
    monkeypatch.setattr(config_mod.settings, "IMPERSO_ALLOWLIST", "localhost")
    with pytest.raises(AppError) as ei:
        run(impersonation_svc.impersonate_fetch("http://localhost:9/a"))
    assert ei.value.code == "E_PROVIDER_FATAL"


# -- audit -----------------------------------------------------------------------
def test_audit_record_shape(listed):
    rec = impersonation_svc.audit_record("https://Example.com/a", "ok", 200)
    assert rec["event"] == "impersonation.fetch"
    assert rec["host"] == "example.com" and rec["profile"] == "chrome"
    assert rec["outcome"] == "ok" and rec["status_code"] == 200 and rec["at"] != ""
    long_err = "x" * 500
    assert len(impersonation_svc.audit_record("u", "failed", 0, long_err)["error"]) == 200


def test_no_proxy_param_in_signature():
    params = inspect.signature(impersonation_svc.impersonate_fetch).parameters
    assert set(params) == {"url", "timeout"}


# -- transport taxonomy via faked session -------------------------------------------
class _FakeHeaders(dict):
    pass


class _FakeResponse:
    def __init__(self, status=200, ctype="text/html", body=b"", location=""):
        self.status_code = status
        self.headers = _FakeHeaders({"content-type": ctype})
        if location:
            self.headers["location"] = location
        self.content = body


class _FakeSession:
    seen_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeSession.seen_kwargs = kwargs
        self._response = _FakeSession.next_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        return self._response


def _patch_net(monkeypatch, response):
    from app.providers.crawl import fetcher as fetcher_mod
    import curl_cffi.requests as curl_requests

    async def _aguard(url):
        return url

    async def _robots(url):
        return True, 0.0

    async def _throttle(host, floor=0.0):
        return None

    monkeypatch.setattr(fetcher_mod, "aguard_url", _aguard)
    monkeypatch.setattr(fetcher_mod, "robots_allowed", _robots)
    monkeypatch.setattr(fetcher_mod, "throttle", _throttle)
    _FakeSession.next_response = response
    _FakeSession.seen_kwargs = {}
    monkeypatch.setattr(curl_requests, "AsyncSession", _FakeSession)


def test_impersonate_success_page_and_kwargs(listed, monkeypatch):
    _patch_net(monkeypatch, _FakeResponse(
        200, "text/html", b"<html><head><title>Hi</title></head><body><p>Hello</p></body></html>"))
    page = run(impersonation_svc.impersonate_fetch("https://example.com/a"))
    assert page["method"] == "impersonate" and page["title"] == "Hi"
    assert "Hello" in page["text"]
    assert _FakeSession.seen_kwargs.get("impersonate") == "chrome"
    assert not any("proxy" in k.lower() for k in _FakeSession.seen_kwargs)


def test_impersonate_403_fatal_429_transient(listed, monkeypatch):
    _patch_net(monkeypatch, _FakeResponse(403, "text/html", b"denied"))
    with pytest.raises(AppError) as ei:
        run(impersonation_svc.impersonate_fetch("https://example.com/a"))
    assert ei.value.code == "E_PROVIDER_FATAL"  # no bypass, no challenge-solving
    _patch_net(monkeypatch, _FakeResponse(429, "text/html", b"slow"))
    with pytest.raises(AppError) as ei2:
        run(impersonation_svc.impersonate_fetch("https://example.com/a"))
    assert ei2.value.code == "E_PROVIDER_TRANSIENT"


def test_impersonate_non_html_skipped(listed, monkeypatch):
    _patch_net(monkeypatch, _FakeResponse(200, "application/pdf", b"%PDF"))
    page = run(impersonation_svc.impersonate_fetch("https://example.com/a.pdf"))
    assert page["skipped"].startswith("non-html:") and page["method"] == "impersonate"


def test_impersonate_redirect_hop_checked(listed, monkeypatch):
    from app.providers.crawl import fetcher as fetcher_mod

    _patch_net(monkeypatch, _FakeResponse(302, "text/html", b"", location="/b"))

    async def _aguard_hop(url):
        if "example.com" in url:
            return url
        raise AssertionError("private hop must be denied by aguard")

    monkeypatch.setattr(fetcher_mod, "aguard_url", _aguard_hop)
    # same-host redirect: second get returns final 200 page
    calls = {"n": 0}

    async def _get(self, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(302, "text/html", b"", location="/b")
        return _FakeResponse(200, "text/html", b"<html><body><p>Done</p></body></html>")

    monkeypatch.setattr(_FakeSession, "get", _get)
    page = run(impersonation_svc.impersonate_fetch("https://example.com/a"))
    assert page["final_url"] == "https://example.com/b" and "Done" in page["text"]


def test_bad_profile_is_fatal(listed, monkeypatch):
    import curl_cffi.requests as curl_requests

    def _boom(**kwargs):
        raise RuntimeError("unknown impersonate target 'bogus'")

    monkeypatch.setattr(curl_requests, "AsyncSession", _boom)
    from app.providers.crawl import fetcher as fetcher_mod

    async def _aguard(url):
        return url

    async def _robots(url):
        return True, 0.0

    async def _throttle(host, floor=0.0):
        return None

    monkeypatch.setattr(fetcher_mod, "aguard_url", _aguard)
    monkeypatch.setattr(fetcher_mod, "robots_allowed", _robots)
    monkeypatch.setattr(fetcher_mod, "throttle", _throttle)
    with pytest.raises(AppError) as ei:
        run(impersonation_svc.impersonate_fetch("https://example.com/a"))
    assert ei.value.code == "E_PROVIDER_FATAL"
