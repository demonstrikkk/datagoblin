"""Free-model intel API contract (TestClient, offline).

Covers the two new endpoints: the registry (which must explain the free-tier
gate rather than hide it) and the fan-out (which must return a partial result
when models fail, because two of them 500 upstream today).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.providers.llm import zen  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture()
def fake_answers(monkeypatch):
    async def _ask(model, prompt, **kw):
        if model in ("mimo-v2.5-free", "muse-spark-1.2-contributor-free"):
            raise AppError("E_PROVIDER_TRANSIENT", "server error (HTTP 500)")
        return {"text": '{"answer": 42}', "model": model, "provider": "opencode",
                "latency_ms": 12, "cost": 0, "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    return _ask


# --- GET /api/models/free -----------------------------------------------

def test_free_models_endpoint_lists_registry(client):
    r = client.get("/api/models/free")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None and body["meta"]["correlation_id"]
    data = body["data"]
    assert data["counts"]["text"] == len(zen.text_models())
    assert data["counts"]["decision"] == 1
    ids = {m["id"] for m in data["text_models"]}
    assert "big-pickle" in ids and "space-bunny-free" in ids
    # The gate must be visible, not something the user discovers by 403ing.
    assert "FreeTierError" in data["gated_note"]
    assert "opencode" in data["transports"]


def test_free_models_endpoint_reports_transport_health(client, monkeypatch):
    async def _avail():
        return {"reachable": True, "base_url": "http://127.0.0.1:4096",
                "enabled": True, "error": ""}

    monkeypatch.setattr("app.providers.llm.opencode.available", _avail)
    data = client.get("/api/models/free").json()["data"]
    assert data["opencode"]["reachable"] is True
    assert data["zen"]["enabled"] is True


def test_free_models_endpoint_never_leaks_the_key(client):
    raw = client.get("/api/models/free").text
    assert "Authorization" not in raw and "Bearer" not in raw


# --- POST /api/intel/ask -------------------------------------------------

def test_intel_ask_returns_partial_results(client, fake_answers):
    r = client.post("/api/intel/ask", json={"prompt": "What is X?",
                                           "json": True})
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["results"]) == len(zen.text_models())
    ok = [x for x in data["results"] if x["ok"]]
    bad = [x for x in data["results"] if not x["ok"]]
    assert len(ok) == len(zen.text_models()) - 2
    assert all(x["error"] for x in bad)
    assert data["consensus"]["answered"] == len(ok)
    assert data["consensus"]["agreement"] == "unanimous"
    assert data["credits_used"] == 1


def test_intel_ask_requires_prompt_or_url(client, fake_answers):
    r = client.post("/api/intel/ask", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E_VALIDATION"


def test_intel_ask_rejects_unknown_model(client, fake_answers):
    r = client.post("/api/intel/ask", json={"prompt": "x", "models": ["gpt-4o"]})
    assert r.status_code == 422
    assert "gpt-4o" in r.json()["error"]["message"]


def test_intel_ask_rejects_bad_models_type(client, fake_answers):
    r = client.post("/api/intel/ask", json={"prompt": "x", "models": [1, 2]})
    assert r.status_code == 422


def test_intel_ask_honours_model_subset(client, fake_answers):
    data = client.post("/api/intel/ask", json={"prompt": "x",
                                               "models": ["big-pickle",
                                                          "space-bunny-free"]}).json()["data"]
    assert data["models"] == ["space-bunny-free", "big-pickle"]
    assert len(data["results"]) == 2


def test_intel_ask_rejects_overlong_prompt(client, fake_answers):
    r = client.post("/api/intel/ask", json={"prompt": "x" * 20001})
    assert r.status_code == 422


def test_intel_ask_all_models_failing_still_200(client, monkeypatch):
    """A total outage is reported as data, not as a 5xx: the caller can still
    see that the question was well-formed and every model refused."""
    async def _ask(model, prompt, **kw):
        raise AppError("E_PROVIDER_TRANSIENT", "down")

    monkeypatch.setattr(zen, "ask", _ask)
    r = client.post("/api/intel/ask", json={"prompt": "x"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["consensus"]["answered"] == 0
    assert all(not x["ok"] for x in data["results"])


def test_intel_ask_provenance_present(client, fake_answers):
    """Every row names the model that answered and how it was reached."""
    data = client.post("/api/intel/ask", json={"prompt": "x",
                                               "models": ["big-pickle",
                                                          "space-bunny-free"]}).json()["data"]
    for row in data["results"]:
        assert row["model"] and row["provider"]
        assert row["transport"] in ("opencode", "direct")
        assert isinstance(row["latency_ms"], int)


# --- URL path (server-side fetch -> HTML/markdown -> text) ---------------

@pytest.fixture()
def captured(monkeypatch):
    """Capture the prompt the models actually receive."""
    seen: dict = {"prompts": []}

    async def _one(url, route, fetch_fn, sem):
        seen["url"] = url
        seen["route"] = route
        return seen.get("page") or {
            "html": "<html><head><title>Fieldwork Report</title></head>"
                    "<body><p>Revenue grew to 4.2 million in 2025.</p></body></html>",
            "final_url": url, "title": "Fieldwork Report", "method": "http",
            "skipped": "", "markdown": ""}

    async def _ask(model, prompt, **kw):
        seen["prompts"].append(prompt)
        return {"text": '{"revenue": "4.2 million"}', "model": model,
                "provider": "opencode", "latency_ms": 3, "cost": 0,
                "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr("app.services.crawler._one", _one)
    monkeypatch.setattr(zen, "ask", _ask)
    return seen


def test_intel_ask_url_feeds_page_text_to_models(client, captured):
    data = client.post("/api/intel/ask", json={
        "url": "https://example.com/report", "question": "What was revenue?",
        "models": ["big-pickle"]}).json()["data"]
    assert captured["route"] == "web:http"
    assert captured["url"] == "https://example.com/report"
    assert "4.2 million" in captured["prompts"][0]
    assert "What was revenue?" in captured["prompts"][0]
    assert data["source"]["chars"] > 0
    assert data["source"]["title"] == "Fieldwork Report"


def test_intel_ask_url_uses_markdown_when_html_absent(client, captured):
    """Regression: the rendered rungs (crawl4ai, jina) return `markdown`, not
    `html`. Reading html only answered 'no readable text' for pages that had
    fetched perfectly."""
    captured["page"] = {"html": "", "markdown": "Rendered body: revenue was 4.2m.",
                        "final_url": "https://example.com/js", "title": "JS App",
                        "method": "crawl4ai", "skipped": ""}
    data = client.post("/api/intel/ask", json={
        "url": "https://example.com/js", "question": "revenue?",
        "models": ["big-pickle"]}).json()["data"]
    assert "4.2m" in captured["prompts"][0]
    assert data["source"]["chars"] > 0


def test_intel_ask_url_skipped_source_is_422(client, captured):
    captured["page"] = {"html": "", "final_url": "https://example.com/x", "title": "",
                        "method": "http", "skipped": "robots-disallowed",
                        "markdown": ""}
    r = client.post("/api/intel/ask", json={"url": "https://example.com/x",
                                           "question": "q"})
    assert r.status_code == 422
    assert "robots" in r.json()["error"]["message"]


def test_intel_ask_url_with_no_readable_text_is_422(client, captured):
    captured["page"] = {"html": "<html><body><script>x=1</script></body></html>",
                        "final_url": "https://example.com/y", "title": "",
                        "method": "http", "skipped": "", "markdown": ""}
    r = client.post("/api/intel/ask", json={"url": "https://example.com/y",
                                           "question": "q"})
    assert r.status_code == 422
    assert "readable text" in r.json()["error"]["message"]


def test_intel_ask_rejects_non_http_url(client, captured):
    for bad in ("file:///etc/passwd", "ftp://example.com/x", "not-a-url"):
        r = client.post("/api/intel/ask", json={"url": bad, "question": "q"})
        assert r.status_code == 422, bad


def test_intel_ask_refuses_unrendered_js_shell(client, captured):
    """Regression, measured on startupblink.com: 483KB of HTML that reduces to
    64 chars because 99% of the document is <script>. When the renderer could
    not run, the thin page was returned silently and ten models reported 'no
    companies found' from a bare <title>. A JS shell that failed to render is an
    infrastructure failure and must never reach a model as if it were content."""
    captured["page"] = {
        "html": "<html><head><title>Top AI Startups in Australia</title></head>"
                "<body>" + "<script>var x=1;</script>" * 4000 + "</body></html>",
        "markdown": "", "final_url": "https://www.startupblink.com/x",
        "title": "Top Artificial Intelligence Startups in Australia", "method": "http",
        "skipped": ""}
    r = client.post("/api/intel/ask", json={"url": "https://www.startupblink.com/x",
                                           "question": "q", "models": ["big-pickle"]})
    assert r.status_code == 422
    msg = r.json()["error"]["message"]
    assert "Render failed" in msg
    assert "JavaScript app" in msg
    assert captured["prompts"] == [], "no model may be asked about an unrendered shell"


def test_intel_ask_allows_rendered_js_shell(client, captured):
    """The same page IS answerable once the renderer runs — 28k chars of real
    markdown. It must not be blocked."""
    captured["page"] = {
        "html": "<html><head><title>T</title></head><body><script>x=1</script></body></html>",
        "markdown": "# 204 Top AI Startups in Australia\n" + ("Acme AI. " * 3000),
        "final_url": "https://www.startupblink.com/x", "title": "T",
        "method": "crawl4ai", "skipped": ""}
    r = client.post("/api/intel/ask", json={"url": "https://www.startupblink.com/x",
                                           "question": "q", "models": ["big-pickle"]})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["source"]["rendered"] is True
    assert r.json()["data"]["source"]["method"] == "crawl4ai"
    assert "Acme AI" in captured["prompts"][0]


def test_intel_ask_allows_genuinely_short_static_page(client, captured):
    """A short but scriptless page is real content, not a failed render."""
    captured["page"] = {
        "html": "<html><head><title>Note</title></head><body><p>Revenue was 4.2m "
                "in 2025, up from 3.1m in 2024 across three regions.</p></body></html>",
        "markdown": "", "final_url": "https://example.com/note", "title": "Note",
        "method": "http", "skipped": ""}
    r = client.post("/api/intel/ask", json={"url": "https://example.com/note",
                                           "question": "q", "models": ["big-pickle"]})
    assert r.status_code == 200, r.text
    assert "4.2m" in captured["prompts"][0]
    assert r.json()["data"]["source"]["thin"] is True


def test_intel_ask_url_prompt_is_capped(client, captured):
    """A huge page must not become an unbounded prompt."""
    captured["page"] = {"html": "<html><body>" + ("word " * 60000) + "</body></html>",
                        "final_url": "https://example.com/big", "title": "Big",
                        "method": "http", "skipped": "", "markdown": ""}
    client.post("/api/intel/ask", json={"url": "https://example.com/big",
                                        "question": "q", "models": ["big-pickle"]})
    assert len(captured["prompts"][0]) < 24000


def test_intel_ask_prompt_and_url_both_given_prefers_prompt(client, captured):
    client.post("/api/intel/ask", json={"url": "https://example.com/r",
                                        "prompt": "Use this instead",
                                        "question": "q", "models": ["big-pickle"]})
    assert captured["prompts"][0].endswith("Use this instead")

