"""ResearchState — explicit typed dict. List fields carry reducers (append, never lose)."""
from typing import Annotated
import operator
from typing_extensions import TypedDict


class ResearchState(TypedDict):
    run_id: str
    goal: str
    entity: str
    fields: list[dict]
    queries: list[str]
    searched_queries: Annotated[list[str], operator.add]
    candidate_urls: Annotated[list[dict], operator.add]
    screened_urls: Annotated[list[str], operator.add]
    accepted_sources: Annotated[list[dict], operator.add]
    extracted_records: Annotated[list[dict], operator.add]
    rejected_records: Annotated[list[dict], operator.add]
    requested_count: int
    valid_count: int
    missing_fields: list[str]
    iteration: int
    max_iterations: int
    last_searched: int
    attempted: int
    decision: str
    decision_reason: str
