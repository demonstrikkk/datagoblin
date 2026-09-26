"""Strict startup-validated settings. Single owner of all env (Phase 1).

Fail-fast: `require()` raises named errors for absent secrets at first use.
Bounds are enforced here so hot paths never parse or trust env.
No module outside core may call os.getenv directly.
"""
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # API
    API_HOST: str = "127.0.0.1"
    API_PORT: int = Field(default=8000, ge=1, le=65535)
    API_WORKERS: int = Field(default=1, ge=1, le=8)
    CORS_ORIGINS: str = "http://localhost:5173"
    VITE_API_URL: str = "http://localhost:8000"
    API_KEY: str = ""

    # Supabase / DB
    DATABASE_URL: str = ""
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
    # Strict reader mode: opencode is THE reader — its failure raises instead
    # of billing cloud fallbacks. Use with a pinned free Zen model
    # (e.g. opencode/muse-spark-1.3-contributor-free). False = cascade.
    OPENCODE_STRICT: bool = False
    # Session answers stream in (agent thinks, tools run); bound the whole wait.
    OPENCODE_TIMEOUT_S: int = Field(default=240, ge=30, le=1200)
    LLM_TIMEOUT_S: int = Field(default=60, ge=5, le=300)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0, le=5)

    # Fetch
    FETCH_HTTP_TIMEOUT_S: int = Field(default=20, ge=1, le=120)
    FETCH_CRAWL_TIMEOUT_S: int = Field(default=90, ge=10, le=600)
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
    RUN_MAX_SEARCH_QUERIES: int = Field(default=5, ge=1, le=8)
    RUN_MAX_RESULTS_PER_QUERY: int = Field(default=5, ge=1, le=20)
    # Deep-crawl ceilings raised for complete-site capture; defaults stay
    # conservative (12 pages / depth 2) — plans opt into more explicitly.
    RUN_MAX_PAGES: int = Field(default=12, ge=1, le=200)
    RUN_MAX_DEPTH: int = Field(default=2, ge=1, le=5)
    RUN_MAX_DOMAIN_PAGES: int = Field(default=10, ge=1, le=50)
    RUN_MAX_REQUESTS: int = Field(default=50, ge=1, le=200)
    RUN_MAX_PAGES_PER_SOURCE: int = Field(default=5, ge=1, le=10)
    RUN_RETRY_COUNT: int = Field(default=1, ge=0, le=3)
    RUN_MAX_RUNTIME_S: int = Field(default=1200, ge=30, le=3600)
    # Pacing between extraction pages: free-tier LLM quotas are per-minute, so
    # back-to-back page calls stall in queue and die at EXTRACT_PAGE_TIMEOUT_S.
    # A short breath between pages keeps calls under the limit (6 RPM at 10s).
    EXTRACT_PAGE_SPACING_S: int = Field(default=10, ge=0, le=120)
    # Phase-5 metering: per-run credit budget. 0 = unlimited (record-only).
    RUN_CREDIT_BUDGET: int = Field(default=0, ge=0, le=100000)
    SUPERVISOR_MAX_ITERATIONS: int = Field(default=3, ge=1, le=5)
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

    # Feature gates
    FEATURE_DOCLING: bool = False
    FEATURE_JINA: bool = False
    FEATURE_SEMANTIC_DEDUP: bool = False
    FEATURE_BROWSER: bool = False
    FEATURE_MCP: bool = False

    # Later stores
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION: str = "datagoblin_l3"

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
