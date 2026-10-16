"""Pydantic domain schemas — mirror contracts/*.schema.json. Single source for API+DB."""
import re
from typing import Any, Literal
from pydantic import BaseModel, Field

FieldType = Literal["string","number","boolean","date","array","url"]
#: `judgment_unavailable` is deliberately distinct from `verified`. It means the
#: quote was found in the page but no judge was reachable to rule on it, which
#: used to be reported as `verified` and inflated every verification summary.
#: `rate_limited` is distinct again: the judge was there and said "wait". That
#: is an operational condition worth retrying, not an absence of evidence.
Verification = Literal["verified", "unverified", "conflicting",
                       "judgment_unavailable", "rate_limited"]


def is_identity_field(name: str) -> bool:
    """Is this field the *subject* of a record, rather than a property of one?

    The distinction decides whether a page-level value may fill it. A listing page
    declares exactly one `Organization` — the site publishing it — and a
    JSON-LD reader in document order reaches it first. So `company_name` on a
    TechCrunch listing page returned "Latest News | TechCrunch", stamped
    `verified` because a structured route is verified by construction.

    That is the worst possible failure: a confident, proven, wrong value. A
    property like `industry` or `founded` describes whatever entity the page is
    about, so page-level data is legitimate for those. An identity field is
    different — it must come from the record, not from the page furniture.
    """
    # `companyName` and `Company Name` reach this as readily as `company_name`,
    # so camelCase is split before the lookup. Plan fields are snake_case today,
    # but a JSON-LD property or a user-authored schema is not bound by that, and
    # a silent miss here means page furniture gets stored as a record again.
    raw = str(name or "").strip()
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    n = raw.lower().replace("-", "_").replace(" ", "_")
    n = re.sub(r"_+", "_", n).strip("_")
    return n in _IDENTITY_FIELDS or n.endswith(("_name", "_names", "_title"))


#: Fields that name the subject of a record.
_IDENTITY_FIELDS = frozenset({
    "company", "companies", "company_name", "organisation", "organization",
    "organisation_name", "organization_name", "startup", "startup_name",
    "business", "business_name", "founder", "founders", "founder_name",
    "founder_names", "person", "person_name", "people", "author", "authors",
    "author_name", "author_names", "employer", "employer_name", "name",
    "names", "product", "product_name", "vendor", "supplier", "customer",
    "client", "publisher", "owner", "operator", "agency", "investor",
    "employee", "candidate", "title",
})


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
    traversal: dict = {"max_pages_per_domain": 10}
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
    """One stored cell: its value, whether the evidence supports it, and where it came from.

    The receipt fields are not decoration. `validator._receipt_for` builds them
    and `RecordRow.model_validate` is the last step before a row is stored — and
    pydantic drops any field this model does not declare. They were not declared,
    so every stored cell lost `read_method`, `confidence`, `certainty` and their
    labels, and the route that actually produced a value was invisible the moment
    it was written.

    That is the same failure as the run counters being dropped in `main._emit`: a
    value computed carefully, stored, and then removed by a model that did not
    declare it. It also made source reputation blind to method — there was no way
    to ask which routes produced the verified cells a host is credited with.

    `extra="allow"` rather than enumerating each field: the receipt is assembled
    by `confidence.receipt` in one place, and a model that had to be updated every
    time that function grew a key is the same trap wearing a different hat.
    """
    model_config = {"extra": "allow"}

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
