"""Stage + event constants — MUST match docs/07 + docs/16 exactly. Do not add states."""
from enum import Enum


class RunStage(str, Enum):
    PLANNING = "PLANNING"
    DISCOVERING = "DISCOVERING"
    FETCHING = "FETCHING"
    REDUCING = "REDUCING"
    EXTRACTING = "EXTRACTING"
    VALIDATING = "VALIDATING"
    DEDUPLICATING = "DEDUPLICATING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: Run *status* may be PARTIAL (records were stored, the budget ran out) while
#: the *stage* is FAILED. There is deliberately no PARTIAL stage: the run did not
#: reach COMPLETED, and inventing a stage for "stopped early" would imply the
#: pipeline had a step it does not have. See docs/16.

LEGAL_TRANSITIONS = {
    "PLANNING": ["DISCOVERING"],
    "DISCOVERING": ["FETCHING"],
    "FETCHING": ["REDUCING"],
    "REDUCING": ["EXTRACTING"],
    "EXTRACTING": ["VALIDATING"],
    "VALIDATING": ["DEDUPLICATING"],
    "DEDUPLICATING": ["FINALIZING"],
    "FINALIZING": ["COMPLETED"],
}

#: Exactly the set the runner emits. `run.created` used to be declared here and
#: in docs/16 but was never sent: run creation is conveyed by the
#: `POST /api/runs` response body, not by an SSE event. `duplicate.detected` and
#: `stage.completed` were likewise declared and never emitted. All three were
#: removed rather than left as a contract nothing honours.
EVENT_TYPES = ["run.partial",
    "stage.started", "stage.progress",
    "source.discovered", "source.fetched",
    "record.extracted", "record.verified", "record.needs_review", "record.rejected",
    "duplicate.merged",
    "run.completed", "run.failed", "run.cancelled"]
