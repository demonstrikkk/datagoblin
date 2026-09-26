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
    attempted: int = Field(default=0, ge=0)
    successful: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    records: int = Field(default=0, ge=0)
    verified: int = Field(default=0, ge=0)
    needs_review: int = Field(default=0, ge=0)


class RunView(BaseModel):
    run_id: str
    status: str
    current_stage: str = ""
    progress: int = Field(default=0, ge=0, le=100)
    counters: Counters = Counters()
    dataset_id: str | None = None
    error: str = ""


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
