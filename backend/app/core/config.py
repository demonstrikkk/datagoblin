"""Strict startup-validated settings. Single owner of all env (Phase 1).

Fail-fast: `require()` raises named errors for absent secrets at first use.
Bounds are enforced here so hot paths never parse or trust env.
No module outside core may call os.getenv directly.
"""
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Absolute path to the repo's .env, found by walking up from this file.
#:
#: `env_file=".env"` is resolved against the *process working directory*, so the
#: whole config — including the OpenCode password — silently became empty
#: whenever the API was started from anywhere other than the repo root. Nothing
#: reported it: `OPENCODE_PASSWORD` defaults to "", the client then omits its
#: Authorization header, and the LLM server answers 401 to every extraction.
#: That is the same symptom as a wrong password and the opposite fix, which is
#: why it is worth removing the possibility rather than documenting the cwd.
_ENV_FILE = next(
    (p / ".env" for p in Path(__file__).resolve().parents
     if (p / ".env").is_file()),
    None,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API
    API_HOST: str = "127.0.0.1"
    API_PORT: int = Field(default=8000, ge=1, le=65535)
    API_WORKERS: int = Field(default=1, ge=1, le=8)
    CORS_ORIGINS: str = "http://localhost:5173"
    VITE_API_URL: str = "http://localhost:8000"
    API_KEY: str = ""
    # Explicit opt-in to running without authentication. The gate fails closed
    # when API_KEY is empty, because an absent key is indistinguishable from a
    # misconfigured deployment, and an open API spends real provider budget.
    # Set this to true only for a deliberately open local instance.
    ALLOW_UNAUTHENTICATED: bool = False

    # Persistence
    # Which store the app writes to. Previously inferred from whether Supabase
    # keys were present, which silently downgraded to JSONL and left a migrated
    # Postgres unused. `postgres` is now the default whenever DATABASE_URL is
    # set; `local` is the explicit opt-in for the JSONL dev store.
    PERSISTENCE: Literal["postgres", "local"] = "postgres"
    DATABASE_URL: str = ""
    # Retained for the REST client's benefit only; the app reads DATABASE_URL.
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""
    DB_POOL_MIN: int = Field(default=1, ge=1, le=10)
    DB_POOL_MAX: int = Field(default=5, ge=1, le=50)
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=15000, ge=1000, le=120000)
    REQUIRE_KEYS_AT_STARTUP: bool = True

    # Discovery
    TAVILY_API_KEY: str = ""
    TAVILY_SEARCH_DEPTH: str = "basic"
    TAVILY_TOPIC: str = "general"

    # LLM
    GEMINI_API_KEY: str = ""
    GEMINI_PLAN_MODEL: str = "gemini-2.5-flash"
    GEMINI_EXTRACT_MODEL: str = "gemini-2.5-flash"
    GROQ_API_KEY: str = ""
    GROQ_FALLBACK_MODEL: str = "openai/gpt-oss-20b"
    # Primary: local OpenCode server (OpenAI-compatible). Serve it with e.g.
    #   OPENCODE_SERVER_PASSWORD=... opencode serve --port 4096
    # then set the same password here. Server down => transient => Groq/Gemini
    # serve instead (no stall, no error). Set false to skip the rung entirely.
    OPENCODE_ENABLED: bool = True
    OPENCODE_BASE_URL: str = "http://127.0.0.1:4096"
    OPENCODE_PASSWORD: str = ""
    OPENCODE_MODEL: str = ""
    # Provider id the server expects in the per-message model pin. Zen models
    # are addressed as opencode/<model-id> in opencode config and
    # {"providerID": "opencode", "modelID": "<id>"} on the wire.
    OPENCODE_PROVIDER_ID: str = "opencode"
    # Strict reader mode: opencode is THE reader — its failure raises instead
    # of billing cloud fallbacks. Use with a pinned free Zen model
    # (e.g. opencode/muse-spark-1.3-contributor-free). False = cascade.
    OPENCODE_STRICT: bool = False
    # Session answers stream in (agent thinks, tools run); bound the whole wait.
    OPENCODE_TIMEOUT_S: int = Field(default=240, ge=30, le=1200)
    LLM_TIMEOUT_S: int = Field(default=60, ge=5, le=300)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0, le=5)

    # --- OpenCode Zen: one key, the whole model gateway --------------------
    # Zen serves OpenAI/Anthropic/Google-shaped REST under /zen/v1. Free-tier
    # models are gated: 7 of the 9 answer 403 FreeTierError ("can only be used
    # from within OpenCode") on direct REST, so those ride the local server
    # (OPENCODE_* above) while ungated ones are called straight from here.
    ZEN_ENABLED: bool = True
    ZEN_API_KEY: str = ""
    ZEN_BASE_URL: str = "https://opencode.ai/zen/v1"
    ZEN_MODEL: str = "space-bunny-free"
    ZEN_TIMEOUT_S: int = Field(default=120, ge=10, le=600)
    ZEN_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    ZEN_MAX_TOKENS: int = Field(default=4000, ge=64, le=64000)
    # Fan-out: ask N free models the same question, compare their answers.
    ZEN_FANOUT_CONCURRENCY: int = Field(default=4, ge=1, le=16)
    # Empty = the built-in free set (see providers/llm/zen.py FREE_MODELS).
    ZEN_FANOUT_MODELS: str = ""
    # Also try Zen's free Jev before the paid OpenRouter judge.
    ZEN_JEV_ENABLED: bool = True
    JEV_ZEN_MODEL: str = "jev-1.13-free"

    #: Drop pages that share no subject term with the run's goal, before
    #: spending an extraction call on them.
    #:
    #: Added after a live run answered "hackathon sponsors in New Delhi" with
    #: records citing an AWS product page and a free-tier page. Those cells were
    #: marked verified, because verification checks that a quote is faithful to
    #: its page -- not that the page is about the question. Both faults produce
    #: confident, well-sourced, irrelevant rows, and only one of them is
    #: detectable by evidence checking.
    #:
    #: Default on because the failure it prevents is silent. The bar is
    #: deliberately low (one shared subject term, body or URL) so it drops
    #: clearly-off-topic pages rather than making a semantic judgement it cannot
    #: make -- see services/relevance.py for why losing rows is worse than
    #: keeping a few.
    EXTRACT_RELEVANCE_GATE: bool = Field(default=True)

    # Fetch
    FETCH_HTTP_TIMEOUT_S: int = Field(default=20, ge=1, le=120)
    FETCH_CRAWL_TIMEOUT_S: int = Field(default=90, ge=10, le=600)

    # Browser reuse. Off by default: without it every page launches and tears
    # down its own browser, which is the behaviour these settings change and
    # the behaviour to fall back to if reuse misbehaves.
    #
    # Reuse is not a free win. It serialises the browser rung onto one owning
    # loop, so four concurrent rendered pages become one at a time. That trade
    # is worth it when a Chromium launch costs more than the page, and is not
    # when it does not -- hence a flag rather than a default-on change.
    REUSE_BROWSER: bool = Field(default=False)
    #: Pages a single browser serves before being recycled. Bounds how long a
    #: slowly-degrading browser can keep being handed new pages.
    REUSE_BROWSER_MAX_PAGES: int = Field(default=25, ge=1, le=1000)
    #: Seconds with no use after which an idle browser is closed, so an idle
    #: process is not holding Chromium open indefinitely.
    REUSE_BROWSER_IDLE_S: float = Field(default=120.0, ge=0, le=3600)
    #: Browsers held open at once. 1 matches the single owning loop; raising it
    #: buys concurrency at the cost of one Chromium per slot.
    REUSE_BROWSER_CONCURRENCY: int = Field(default=1, ge=1, le=4)
    # Phase-6 thinness gate: static pages with less clean text than this AND a
    # script tag escalate to the renderer when one remains untried.
    FETCH_THIN_CHARS: int = Field(default=500, ge=100, le=5000)
    FETCH_USER_AGENT: str = "DATAGOBLIN/0.1 (permitted-sources bot)"
    JINA_API_KEY: str = ""
    # Phase-4 deterministic selectors: directory of checked-in domain schemas.
    # Empty = <backend>/selectors.
    SELECTORS_DIR: str = ""
    # Phase 2 impersonation allowlist: comma-separated host suffixes
    # (e.g. "example.com" also covers "www.example.com"). Empty = disabled:
    # impersonation NEVER fires without an explicit entry. No proxies, no
    # challenge-solving — curl_cffi TLS impersonation only (policy §5).
    IMPERSO_ALLOWLIST: str = ""
    IMPERSO_IMPERSONATE: str = "chrome"

    # Decisions (Jev via OpenRouter; TYPESAFE_API_KEY accepted as alias because
    # existing .env files carry the OpenRouter key under that name)
    TYPESAFE_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    JEV_MODEL: str = "~typesafe/jev-latest"

    # Budgets (env may only tighten below code maxima; Runner clamps)
    # NOTE: `RUN_MAX_SEARCH_QUERIES`, `RUN_MAX_REQUESTS` and
    # `RUN_MAX_PAGES_PER_SOURCE` used to live here with zero readers anywhere in
    # `app/` — knobs that turned nothing. They were removed rather than left to
    # mislead; the live query budget is `SUPERVISOR_MAX_QUERIES`, the page
    # budget is `RUN_MAX_PAGES`.
    RUN_MAX_RESULTS_PER_QUERY: int = Field(default=5, ge=1, le=20)
    # Deep-crawl ceilings raised for complete-site capture; defaults stay
    # conservative (12 pages / depth 2) — plans opt into more explicitly.
    RUN_MAX_PAGES: int = Field(default=12, ge=1, le=200)
    RUN_MAX_DEPTH: int = Field(default=2, ge=1, le=5)
    RUN_MAX_DOMAIN_PAGES: int = Field(default=10, ge=1, le=50)
    RUN_RETRY_COUNT: int = Field(default=1, ge=0, le=3)
    RUN_MAX_RUNTIME_S: int = Field(default=1200, ge=30, le=3600)
    # Pacing between extraction pages: free-tier LLM quotas are per-minute, so
    # back-to-back page calls stall in queue and die at EXTRACT_PAGE_TIMEOUT_S.
    # A short breath between pages keeps calls under the limit (6 RPM at 10s).
    EXTRACT_PAGE_SPACING_S: int = Field(default=10, ge=0, le=120)
    # How many pages may be extracted at once. Pages used to be extracted
    # strictly one after another, so a run's extraction cost was the SUM of
    # every page's LLM latency and 8 pages could not finish inside the runtime
    # budget however much was already done. Kept low deliberately: the free tier
    # is rate limited, and the transport retries 429s, but inviting a storm to
    # save wall time is a bad trade.
    EXTRACT_PAGE_CONCURRENCY: int = Field(default=6, ge=1, le=8)
    # Hard ceiling on judge calls per run. Verification asks a judge about every
    # field of every extracted record, and that count is a function of how much
    # the extractor found: a live run reached 728 records, which is thousands of
    # round trips and a guaranteed budget overrun. Past the cap, fields are
    # reported `judgment_unavailable` - kept, but never counted as verified.
    # 0 = unlimited.
    RUN_MAX_JUDGE_CALLS: int = Field(default=400, ge=0, le=100000)
    # Retries for a throttled (HTTP 429) judge, with exponential backoff.
    # Quitting on the first 429 made a busy run report every field as
    # unverified, which is indistinguishable from having no evidence.
    JEV_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    # Phase-5 metering: per-run credit budget. 0 = unlimited (record-only).
    RUN_CREDIT_BUDGET: int = Field(default=0, ge=0, le=100000)
    SUPERVISOR_MAX_ITERATIONS: int = Field(default=3, ge=1, le=5)
    # Re-query when every candidate was blocked or filtered out. Queries aimed at
    # official data sources fail often: search engines rank .gov and primary
    # registries highest, and those enforce the strictest robots.txt. So the
    # rejected domains are excluded and the search is repeated - bounded, because
    # a topic can genuinely have no accessible source and an unbounded loop
    # would drain the search budget discovering that the slow way.
    DISCOVERY_MAX_REQUERIES: int = Field(default=2, ge=0, le=5)
    # At most this many -site: terms per re-query, so the query stays inside a
    # sane length instead of growing with every rejected host.
    DISCOVERY_MAX_SITE_EXCLUSIONS: int = Field(default=6, ge=0, le=25)
    SUPERVISOR_MAX_QUERIES: int = Field(default=8, ge=1, le=12)

    # Caps
    REDUCE_MAX_CHARS: int = Field(default=30000, ge=1000, le=200000)
    EXTRACT_MAX_CHARS: int = Field(default=12000, ge=1000, le=100000)
    # Per-page extraction ceiling: one stalled LLM call must never eat the
    # whole RUN_MAX_RUNTIME_S. Worst-case chain per page (~60 groq + 3x60
    # gemini retries) exceeds this, so the runner cuts the page off and
    # continues — slow pages yield nothing instead of killing the run.
    EXTRACT_PAGE_TIMEOUT_S: int = Field(default=150, ge=30, le=600)

    # SSE / export
    SSE_PING_S: int = Field(default=15, ge=5, le=120)
    EXPORT_MAX_ROWS: int = Field(default=5000, ge=1, le=100000)
    EXPORT_CSV_ENCODING: str = "utf-8"
    # Phase-3 column pin: comma-separated export column names. Empty = schema
    # names (current behavior). Set to freeze headers against schema drift.
    EXPORT_FIELDS: str = ""
    # Phase-3 item pipeline: max concurrent per-record validations (Jev calls).
    ITEM_CONCURRENCY: int = Field(default=8, ge=1, le=32)

    # Feature gates. Only flags with a wired rung live here: `FEATURE_DOCLING`
    # leads document routes and `FEATURE_JINA` trails web routes (see
    # `crawler._methods_for`); `FEATURE_SEMANTIC_DEDUP` fails fast with a named
    # error until a provider exists. The rest of the roadmap flags
    # (`FEATURE_BROWSER`, `FEATURE_MCP`) and the Qdrant store settings were
    # removed with zero readers — a flag that gates nothing is a promise the
    # code does not keep.
    FEATURE_DOCLING: bool = False
    FEATURE_JINA: bool = False
    FEATURE_SEMANTIC_DEDUP: bool = False

    # Observability
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_TRACING: bool = False
    LANGSMITH_PROJECT: str = "datagoblin"
    CORRELATION_HEADER: str = "X-Request-ID"

    @field_validator("TAVILY_SEARCH_DEPTH")
    @classmethod
    def _depth(cls, v: str) -> str:
        if v not in ("basic", "advanced"):
            raise ValueError("TAVILY_SEARCH_DEPTH must be basic|advanced")
        return v

    @field_validator("TAVILY_TOPIC")
    @classmethod
    def _topic(cls, v: str) -> str:
        if v not in ("general", "news", "finance"):
            raise ValueError("TAVILY_TOPIC must be general|news|finance")
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("LOG_LEVEL must be DEBUG|INFO|WARNING|ERROR")
        return v

    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def require(self, name: str) -> str:
        value = getattr(self, name, "")
        if not value:
            raise RuntimeError(f"Missing required configuration: {name} (see .env.example)")
        return value


settings = Settings()
