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

EVENT_TYPES = ["run.created","stage.started","stage.progress","source.discovered","source.fetched",
    "record.extracted","record.verified","record.rejected","duplicate.detected","duplicate.merged",
    "stage.completed","run.completed","run.failed","run.cancelled"]
