"""Pydantic domain schemas — mirror contracts/*.schema.json. Single source for API+DB."""
from typing import Any, Literal
from pydantic import BaseModel, Field

FieldType = Literal["string","number","boolean","date","array","url"]
#: `judgment_unavailable` is deliberately distinct from `verified`. It means the
#: quote was found in the page but no judge was reachable to rule on it, which
#: used to be reported as `verified` and inflated every verification summary.
Verification = Literal["verified", "unverified", "conflicting", "judgment_unavailable"]


class FieldSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    type: FieldType
    description: str = ""
    required: bool = False


class WorkflowPlan(BaseModel):
    goal: str
    entity: str
    requested_count: int = 15
    max_results: int = 15
    fields: list[FieldSpec]
    search_queries: list[str] = Field(min_length=1, max_length=5)
    seed_domains: list[str] = []
    seed_urls: list[str] = []
    source_types: list[str] = []
    traversal: dict = {"max_pages_per_domain": 3}
    validation_rules: list[str] = []
    dedupe_keys: list[str] = []
    allowed_sources: list[str] = []
    max_pages: int = 12


class SourceRef(BaseModel):
    url: str
    title: str = ""
    quote: str
    retrieved_at: str  # ISO-8601
    # Phase-1 evidence chain (all optional; pin the quote to preserved text):
    reference_id: str = ""  # <n> citation marker from Crawl4AI references
    start: int | None = None  # char offset of quote in the preserved source text
    end: int | None = None
    # The stored page this claim came from. `start`/`end` above are only
    # meaningful against text that still exists; this is how a reader gets to
    # it. Empty when the run stored no evidence (probe/tests, no repo method).
    page_id: str = ""
    content_hash: str = ""  # re-fetch and confirm the page has not changed


class ProvenanceField(BaseModel):
    value: Any | None
    verification_status: Verification
    source: SourceRef


class RunEvent(BaseModel):
    type: str
    run_id: str
    stage: str
    message: str
    progress: int = 0
    timestamp: str
    data: dict = {}
