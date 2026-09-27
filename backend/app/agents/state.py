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
    #: What discovery knows, rendered for the coverage judge. Without it the
    #: judge was asked about 0 valid records out of 20 and told nothing else,
    #: so it had no basis beyond a count it could not influence.
    evidence_summary: str
    #: Hosts rejected by the robots gate or the per-domain cap. Fed back into
    #: search as -site: exclusions so a re-query surfaces secondary sources
    #: instead of the same blocked official portal.
    blocked_domains: list[str]
    #: Re-queries spent so far, bounded by DISCOVERY_MAX_REQUERIES.
    requery_count: int
    #: Terminal signal: every candidate was blocked or filtered and the
    #: re-query budget is spent, so the run stops instead of looping.
    all_blocked: bool
