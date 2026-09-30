"""Run/dataset/API schemas. Transport envelope invariant: exactly one of data/error."""
from typing import Any, Generic, TypeVar
from pydantic import BaseModel, Field

from app.schemas.plan import ProvenanceField, RunEvent

__all__ = ["ProvenanceField", "RunEvent", "Envelope", "Counters", "RunView",
           "DatasetView", "RecordRow"]

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    data: T | None = None
    error: dict | None = None
    meta: dict = {}

    def model_post_init(self, _ctx: Any) -> None:
        if (self.data is None) == (self.error is None):
            raise ValueError("Envelope requires exactly one of data/error")


class Counters(BaseModel):
    """Fetch, record and verification counts for one run or dataset.

    The verification names are records, not fields: `verified` used to mean both,
    and a run with 300 records and 5 proven fields reported `verified: 300`. The
    field-level figures are separate keys below and are never folded in.

    Every key here is a key `main._emit` actually builds. The two the event
    handler constructs that this model did not declare — `fields_verified`,
    `records_fully_verified` and their siblings — were silently dropped by
    pydantic, so `GET /api/runs/{id}` reported `verified: 0, needs_review: 0` for
    every run in the database.
    """
    attempted: int = Field(default=0, ge=0)
    successful: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    records: int = Field(default=0, ge=0)
    verified: int = Field(default=0, ge=0)
    needs_review: int = Field(default=0, ge=0)
    # Field-level, kept apart on purpose.
    fields_verified: int = Field(default=0, ge=0)
    fields_unverified: int = Field(default=0, ge=0)
    fields_judgment_unavailable: int = Field(default=0, ge=0)
    fields_rate_limited: int = Field(default=0, ge=0)
    records_fully_verified: int = Field(default=0, ge=0)
    records_needing_review: int = Field(default=0, ge=0)


class RunView(BaseModel):
    run_id: str
    status: str
    current_stage: str = ""
    progress: int = Field(default=0, ge=0, le=100)
    counters: Counters = Counters()
    dataset_id: str | None = None
    error: str = ""
    # `partial` and `no_yield_reason` were computed on every terminal event, with
    # comments explaining why a truncated run must be distinguishable from a
    # completed one — and then dropped, because this model had no such fields, so
    # no endpoint could report either. A run that stored only what it verified
    # was indistinguishable from a run that verified everything.
    partial: bool = False
    no_yield_reason: str = ""


class DatasetView(BaseModel):
    id: str
    run_id: str
    name: str
    fields_schema: list[dict] = Field(default=[], alias="schema")
    record_count: int = Field(default=0, ge=0)
    counts: Counters = Counters()
    created_at: str = ""

    model_config = {"populate_by_name": True}


class RecordRow(BaseModel):
    fields: dict[str, ProvenanceField]
