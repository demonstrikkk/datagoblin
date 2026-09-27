"""Prompt -> WorkflowPlan. Instructor (validated, retrying) primary; direct-SDK
structured call second; rule-based deterministic compiler third (marked
provider=rule-based); generic fallback last (marked provider=fallback)."""
import re
from app.core.config import settings
from app.core.errors import validation
from app.schemas.plan import WorkflowPlan

_ENTITY_HINTS = [  # tie-break order: specific entities before generic company
    ({"job", "jobs", "hiring", "engineer", "engineers", "role", "roles", "salary"}, "job"),
    ({"conference", "conferences", "summit", "event", "events", "sponsor",
      "sponsors", "sponsorship"}, "event"),
    ({"restaurant", "restaurants", "hotel", "hotels"}, "restaurant"),
    ({"startup", "startups", "saas", "company", "companies", "firm", "firms"}, "company"),
]

_FIELD_HINTS = [
    ({"founder", "founders", "ceo", "cto"}, ("founder", "string", "Founder name(s)")),
    ({"funding", "funded", "raised", "raise", "investment", "valuation"}, ("funding_stage", "string", "Funding stage / amount")),
    ({"website", "site", "url", "domain", "homepage"}, ("website", "url", "Official website")),
    ({"salary", "pay", "compensation", "ctc"}, ("salary", "string", "Salary / compensation")),
    ({"location", "city", "country", "london", "india", "remote", "address", "where"}, ("location", "string", "Location")),
    ({"date", "dates", "when", "founded", "year"}, ("date", "string", "Relevant date")),
    ({"organizer", "organiser", "host"}, ("organizer", "string", "Organizer")),
    ({"role", "roles", "title", "position", "job", "jobs", "hiring"}, ("open_roles", "array", "Open roles / titles")),
    ({"employee", "employees", "team", "size", "headcount"}, ("employees", "string", "Team size")),
    ({"email", "contact", "phone"}, ("contact", "string", "Contact")),
]


def _rule_plan(prompt: str) -> dict:
    """Genuine deterministic compiler: count/entity/fields/queries inferred from the
    prompt text itself (no LLM, no hardcoding to any demo). Same contract as LLM plans."""
    text = (prompt or "").strip()
    low = text.lower()
    tokens = re.findall(r"[a-z]+", low)
    words = set(tokens)
    pos = {}
    for i, w in enumerate(tokens):
        pos.setdefault(w, i)

    def _key(item):
        hints, name = item
        hit = words & hints
        if not hit:
            return (0, 0)
        return (len(hit), -min(pos[w] for w in hit))  # hits first, first-mention wins ties

    entity = max(_ENTITY_HINTS, key=_key)[1] if any(words & h for h, _ in _ENTITY_HINTS) else "item"

    m = re.search(r"\b(\d{1,3})\b", text)
    count = max(1, min(int(m.group(1)), 50)) if m else 15

    entity = max(_ENTITY_HINTS, key=_key)[1] if any(words & h for h, _ in _ENTITY_HINTS) else "item"

    name = "company_name" if entity == "company" else f"{entity}_name"
    fields = [{"name": name, "type": "string", "description": f"{entity.title()} name",
               "required": True}]
    seen = {name}
    for hints, (fname, ftype, desc) in _FIELD_HINTS:
        if words & hints and fname not in seen:
            seen.add(fname)
            fields.append({"name": fname, "type": ftype, "description": desc,
                           "required": False})

    loc = next((w for w in ("london", "india", "remote", "usa", "europe") if w in words), "")
    base = re.sub(r"\s+", " ", text)[:100].strip() or entity
    queries = [base]
    if loc or entity != "item":
        queries.append(f"{entity} {loc}".strip())
    if len(fields) > 2:
        queries.append(f"{entity} {fields[1]['name'].replace('_', ' ')} {loc}".strip())
    queries = [q for q in dict.fromkeys(queries) if q][:3] or [entity]

    return {"goal": text[:500], "entity": entity, "requested_count": count,
            "max_results": count,
            "fields": fields, "search_queries": queries, "seed_domains": [],
            "seed_urls": [], "source_types": [],
            "traversal": {"max_pages_per_domain": 3},
            "validation_rules": [f"{name} must exist",
                                 "every non-null field must have evidence"],
            "dedupe_keys": [name], "allowed_sources": [], "max_pages": 12}


def _fallback_plan(prompt: str) -> dict:
    words = (prompt or "")[:200]
    return {"goal": words, "entity": "company", "requested_count": 15, "max_results": 15,
            "fields": [{"name": "company_name", "type": "string",
                        "description": "Name", "required": True}],
            "search_queries": [words[:100] or "general"], "seed_domains": [],
            "seed_urls": [], "source_types": [], "traversal": {"max_pages_per_domain": 3},
            "validation_rules": ["company_name must exist"],
            "dedupe_keys": ["company_name"], "allowed_sources": [], "max_pages": 12}


async def _instructor_plan(prompt: str) -> tuple[dict, str] | None:
    """Instructor from_provider + response_model + max_retries (verified API).
    None when unavailable (no package/key) or when validation keeps failing."""
    if not settings.GEMINI_API_KEY:
        return None
    try:
        import asyncio

        from instructor import from_provider
        client = from_provider(f"google/{settings.GEMINI_PLAN_MODEL}", async_client=True,
                               api_key=settings.GEMINI_API_KEY)
        plan = await asyncio.wait_for(client.create(
            messages=[{"role": "user",
                       "content": "Convert to a WorkflowPlan. "
                                  f"Business data request: {prompt[:2000]}"}],
            response_model=WorkflowPlan, max_retries=1), timeout=120)
        return plan.model_dump(), "instructor:gemini"
    except Exception:
        return None


#: How many fields may be required at once. A record is dropped when a required
#: field has no evidence, so a plan requiring everything guarantees an empty
#: dataset. Two is enough to identify a record; the rest are enrichment.
MAX_REQUIRED_FIELDS = 2


def _cap_required(plan: dict) -> dict:
    """Demote surplus equired\ flags, keeping the identifying ones.

    Applied to every rung, LLM and rule-based alike, so a plan can never be
    unpassable by construction. A measured run marked all 6 generated fields
    required and the validation gate then dropped all 24 extracted records,
    almost all of them solely for a missing \website\. An empty dataset is a
    worse failure than a missing optional column.
    """
    fields = plan.get("fields") or []
    required = [f for f in fields if f.get("required")]
    if len(required) <= MAX_REQUIRED_FIELDS:
        return plan
    # Keep the first ones in declaration order: a planner lists the identifying
    # attribute first in practice, and stable order beats guessing importance
    # from the field name.
    for f in required[MAX_REQUIRED_FIELDS:]:
        f["required"] = False
    return plan


async def compile_plan(prompt: str, llm: object = None) -> tuple[dict, str]:
    """Returns (plan_dict, provider). Raises E_VALIDATION on empty prompt."""
    if not (prompt or "").strip():
        raise validation("Prompt must not be empty")
    if not settings.OPENCODE_STRICT:
        hit = await _instructor_plan(prompt)
        if hit is not None:
            return hit
    # Strict reader mode: the injected llm (opencode) is the ONLY model rung —
    # the hardwired Gemini instructor above never fires, so no cloud quota
    # can burn during planning. Without an llm, straight to rule-based.
    if llm is not None:
        try:
            out = await llm(
                "Convert to WorkflowPlan JSON {goal, entity, requested_count<=50, fields[{name snake_case, "
                "type: string|number|boolean|date|array|url, description, required}], "
                f"search_queries[1..5], dedupe_keys, max_pages<=15, allowed_sources[]}}. Prompt: {prompt[:2000]}\n"
                # A field is only required if EVERY page is expected to carry it
                # with a quotable quote. Validation drops any record missing a
                # required field, so marking all of them required makes a plan
                # unpassable by construction: a live run marked 6 of 6 required
                # and the gate then dropped all 24 extracted records, almost
                # all for an absent `website`. Require the identifying field(s);
                # leave enrichments optional.
                "IMPORTANT: set required=true ONLY for the fields that identify "
                "the record (typically the name/company/title). Set required=false "
                "for enrichment fields like website, revenue, dates, counts, which "
                "are frequently absent from a page. Never mark every field required.",
                WorkflowPlan.model_json_schema())
            data = out.get("data", out) if isinstance(out, dict) else {}
            plan = WorkflowPlan(**data).model_dump()
            return _cap_required(plan), (out.get("provider", "llm")
                                         if isinstance(out, dict) else "llm")
        except Exception:
            pass  # fall through to rule-based compiler (honest, marked)
    try:
        return _cap_required(WorkflowPlan(**_rule_plan(prompt)).model_dump()), "rule-based"
    except Exception:
        return _fallback_plan(prompt), "fallback"


def coerce_plan(data: dict) -> WorkflowPlan:
    try:
        return WorkflowPlan(**data)
    except Exception as e:
        raise validation(f"Invalid WorkflowPlan: {e}")
