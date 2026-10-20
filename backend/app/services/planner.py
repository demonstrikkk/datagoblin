"""Prompt -> WorkflowPlan. Instructor (validated, retrying) primary; direct-SDK
structured call second; rule-based deterministic compiler third (marked
provider=rule-based); generic fallback last (marked provider=fallback)."""
import re
from urllib.parse import urlparse

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
            "traversal": {"max_pages_per_domain": 10},
            "validation_rules": [f"{name} must exist",
                                 "every non-null field must have evidence"],
            "dedupe_keys": [name], "allowed_sources": [], "max_pages": 12}


def _fallback_plan(prompt: str) -> dict:
    words = (prompt or "")[:200]
    return {"goal": words, "entity": "company", "requested_count": 15, "max_results": 15,
            "fields": [{"name": "company_name", "type": "string",
                        "description": "Name", "required": True}],
            "search_queries": [words[:100] or "general"], "seed_domains": [],
            "seed_urls": [], "source_types": [], "traversal": {"max_pages_per_domain": 10},
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
        return _finalize(plan.model_dump(), prompt), "instructor:gemini"
    except Exception:
        return None


#: How many fields may be required at once. A record is dropped when a required
#: field has no evidence, so a plan requiring everything guarantees an empty
#: dataset. Two is enough to identify a record; the rest are enrichment.
#:
#: This is a ceiling, not the policy. A field is only kept required if it is one
#: of the plan's identity fields (`dedupe_keys`) -- the attributes that say what
#: a record *is*. Enrichment a query happens to ask for (a CEO name, a revenue
#: figure) is not identity: a page can legitimately lack it, and requiring it
#: drops the whole record. Measured: a live run marked `company_name` and
#: `chief_executive` required; the pages held company names but no quotable CEO,
#: so the gate dropped all 92 extracted records and stored an empty dataset.
MAX_REQUIRED_FIELDS = 2

# Total fields a plan may carry, whatever the planner proposed.
#
# Cost is fields x pages, so this number sets a run's price. 22 imagined fields
# is what put a real run past its runtime budget; 12 is enough for a schema a
# person would actually read and use.
MAX_TOTAL_FIELDS = 12


def _cap_required(plan: dict) -> dict:
    r"""Restrict `required` to the plan's identity fields.

    Applied to every rung, LLM and rule-based alike, so a plan can never be
    unpassable by construction. Two measured failures drove this:

    * A run marked all 6 generated fields required and the validation gate then
      dropped all 24 extracted records, almost all for a missing `website`.
    * A later run marked `company_name` and `chief_executive` required; the
      pages held company names but no quotable CEO, so the gate dropped all 92
      extracted records. Capping at two fields did not help -- both required
      fields were within the cap.

    The invariant is narrower than a count: only identity (`dedupe_keys`) is
    what a record cannot exist without. Anything else a query asks for is
    enrichment and may be absent from a given page. The `MAX_REQUIRED_FIELDS`
    ceiling still applies in case a plan declares many identity fields.
    """
    fields = plan.get("fields") or []
    required = [f for f in fields if f.get("required")]
    if not required:
        return plan
    identity = _identity_names(plan)
    keep = [f for f in required if str(f.get("name") or "") in identity]
    if not keep:
        # No identity information to anchor on: keep the first declared
        # required field, the one a planner lists as the record's subject.
        keep = required[:1]
    keep = keep[:MAX_REQUIRED_FIELDS]
    keep_ids = {id(f) for f in keep}
    for f in required:
        if id(f) not in keep_ids:
            f["required"] = False
    return plan


def _cap_total_fields(plan: dict) -> dict:
    r"""Cap the *total* field count, keeping the ones a dataset is identified by.

    `_cap_required` bounds how many fields can be mandatory but nothing bounded
    how many exist. A vague prompt about AI engineering jobs produced 22 fields —
    `contact_email`, `apply_deadline_status`, `company_revenue`,
    `company_funding_stage` — and every one of them is extracted and validated
    against every page. That is the arithmetic behind a run that sat in planning
    for minutes and then overran its runtime: cost is fields x pages, so the
    planner's imagination sets the run's price.

    Keeping the identity fields and the fields the prompt actually named is also
    the honest outcome. A schema nobody asked for is not a schema the user
    wanted; it is a guess that happens to be expensive.

    Applied on every path, LLM and rule-based alike.
    """
    fields = plan.get("fields") or []
    if len(fields) <= MAX_TOTAL_FIELDS:
        return plan

    keep, seen = [], set()
    # Identity first: dedupe keys and required fields are what make the output a
    # dataset rather than a list of text.
    ranked = ([f for f in fields if f.get("name") in _identity_names(plan)]
              + [f for f in fields if f.get("required")]
              + list(fields))
    for f in ranked:
        name = str(f.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        keep.append(f)
        if len(keep) >= MAX_TOTAL_FIELDS:
            break

    dropped = [str(f.get("name")) for f in fields if f.get("name") not in seen]
    plan["fields"] = keep[:MAX_TOTAL_FIELDS]
    if dropped:
        # Recorded on the plan rather than logged: the plan is returned to the
        # caller, so this is where the user sees which columns they did not get
        # and can ask for one by name.
        plan["dropped_fields"] = dropped
    return plan


def _identity_names(plan: dict) -> set:
    return {str(n) for n in (plan.get("dedupe_keys") or []) if n}


def _has_enrichment_fields(plan: dict) -> bool:
    """Does this plan ask for anything beyond the record's identity?

    An identity-only plan (a list of names) is served by listing pages. As soon
    as a *property* is requested -- a CEO, a website, a headcount -- the value
    usually lives on a per-entity detail page, which the crawl has to be allowed
    to reach and pay for.
    """
    identity = _identity_names(plan)
    for f in (plan.get("fields") or []):
        name = str((f or {}).get("name") or "")
        if name and name not in identity:
            return True
    return False


#: How many field probes may be folded into a plan's five search queries.
FIELD_PROBE_MAX = 2

#: Field-name tokens -> the phrase a search engine needs to find a page that
#: carries that field. Ordered, and the first matching row wins, because a field
#: carries several tokens (`hq_city` is `hq` + `city`).
#:
#: Generic fields are absent on purpose. A plain `location` means something
#: different on a job dataset than on a company one, and a probe is only worth a
#: search when the concept is specific enough to name.
_FIELD_PROBE_HINTS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"founder", "founders", "cofounder", "cofounders"}), "founder"),
    (frozenset({"ceo", "chief", "executive", "managing", "director",
                "directors", "leadership"}),
     "chief executive"),
    (frozenset({"website", "site", "homepage", "url", "domain"}),
     "official website"),
    (frozenset({"employee", "employees", "headcount", "staff", "team", "size"}),
     "number of employees"),
    (frozenset({"hq", "headquarters", "city", "town"}), "headquarters city"),
    (frozenset({"country"}), "headquarters country"),
    (frozenset({"industry", "sector", "focus"}), "industry"),
    (frozenset({"revenue", "turnover", "sales", "income"}), "revenue"),
    (frozenset({"founded", "founding", "year"}), "founded year"),
    (frozenset({"funding", "round", "valuation", "raised", "investment"}),
     "funding round"),
    (frozenset({"contact", "email", "phone"}), "contact"),
)

_FIELD_WORD = re.compile(r"[a-z0-9]+")


def _name_tokens(name: object) -> set[str]:
    return {w for w in _FIELD_WORD.findall(str(name or "").lower()) if len(w) > 1}


def _plural_fold(tokens: set[str]) -> set[str]:
    """Fold a trailing plural so "CEOs" answers a "ceo" hint.

    Both sides are folded: the hint rows spell out the words a page uses, and a
    plan whose query says "CEOs" has already asked for the CEO. Enumerating
    plurals in the table would work until the next field is added.
    """
    return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in tokens}


def _probe_hint(name: str) -> tuple[frozenset[str], str]:
    """The hint row a field name matches, including the words pages use for it.

    The row's own token set is what the "already asked" test compares against.
    Comparing the identifier alone would miss the live plan exactly: its queries
    said "CEOs" and "headquarters", not "chief"/"hq", so it would have spent both
    probe slots re-asking for fields it already asked for and probed neither
    website nor headcount -- the two columns that were actually empty.
    """
    tokens = _name_tokens(name)
    for hints, phrase in _FIELD_PROBE_HINTS:
        if tokens & hints:
            return hints, phrase
    return frozenset(), ""


def field_phrase(name: str) -> str:
    """The words a page uses for a field, for a search query.

    `chief_executive` is an identifier; nobody writes it. The automatic gap-fill
    pass searched the literal identifier for eight entities and fetched nothing
    ("chief_executive" matches almost no page). Falls back to the identifier for
    a field the vocabulary does not know, which is still better than dropping it.
    """
    return _probe_hint(str(name or ""))[1] or str(name or "")


def _field_probe_queries(plan: dict) -> list[str]:
    """One query per requested field that no existing query already names.

    Discovery can only surface pages for what it searches for. When a plan asks
    for a CEO and every query is about the market ("logistics companies in
    Europe"), the pages that carry the CEO are never candidates -- and no amount
    of crawl depth recovers a page nobody looked for.

    Skipped when the field is already the subject of a query, which is the case
    the live logistics run actually hit: its queries already said "chief
    executive" and "headquarters", so probes would have been duplicate spend.
    """
    queries = [str(q).strip() for q in (plan.get("search_queries") or [])
               if str(q).strip()]
    asked = " ".join(queries).lower()
    asked_words = _plural_fold(set(_FIELD_WORD.findall(asked)))
    subject = (min(queries, key=len) if queries
               else str(plan.get("goal") or plan.get("entity") or "").strip())[:80]
    if not subject:
        return []
    identity = _identity_names(plan)
    out: list[str] = []
    for f in (plan.get("fields") or []):
        name = str((f or {}).get("name") or "")
        if not name or name in identity:
            continue
        hints, phrase = _probe_hint(name)
        if not phrase:
            continue
        if _plural_fold(hints | _name_tokens(name)) & asked_words:
            continue
        q = f"{subject} {phrase}".strip()
        if q and q.lower() not in asked and q not in out:
            out.append(q)
    return out


def _merge_field_probes(plan: dict) -> dict:
    """Fold probes into `search_queries`, never past the schema's five.

    When the list is full, the *shortest* queries yield their slots. Dropping the
    tail instead removed the most specific queries: on the live logistics plan
    the last query named five actual companies, while the shortest was the
    generic "largest companies" phrase the probe is built from anyway.
    """
    probes = _field_probe_queries(plan)[:FIELD_PROBE_MAX]
    if not probes:
        return plan
    existing = [str(q).strip() for q in (plan.get("search_queries") or [])
                if str(q).strip()]
    room = max(0, 5 - len(probes))
    survivors = sorted(range(len(existing)),
                       key=lambda i: (-len(existing[i]), i))[:room]
    keep = [existing[i] for i in sorted(survivors)]
    plan["search_queries"] = list(dict.fromkeys(keep + probes))[:5]
    return plan


def _entity_traversal(plan: dict) -> dict:
    """Give a property-bearing plan the reach its fields require.

    Measured on a live entity run: the crawl queued 49 same-host children and
    fetched none of them because a 10-page budget was spent on the seeds. The
    identity columns were 100% filled from listing pages; the per-entity columns
    (CEO 2/17, website 1/17) were exactly the pages left in that queue.

    So an entity plan reserves enough budget for one detail page per requested
    record, plus a few seeds. The ceiling stays `RUN_MAX_PAGES`; this only asks
    for more than a flat `max_pages: 12` when the schema needs it.
    """
    if not _has_enrichment_fields(plan):
        return plan
    traversal = dict(plan.get("traversal") or {})
    traversal["max_pages_per_domain"] = max(
        int(traversal.get("max_pages_per_domain") or 0), 15)
    traversal["max_children_per_page"] = max(
        int(traversal.get("max_children_per_page") or 0), 12)
    plan["traversal"] = traversal
    requested = int(plan.get("requested_count") or plan.get("max_results") or 15)
    want = requested + 6
    plan["max_pages"] = min(max(int(plan.get("max_pages") or 0), want),
                            settings.RUN_MAX_PAGES)
    return plan


def _requested_allowed_sources(prompt: str, entries) -> list[str]:
    """Keep only the allowlist entries the *prompt itself* named.

    `allowed_sources` is not advice. It is a hard filter in two places: Tavily's
    `include_domains` and the traversal allowlist. So a domain the planner
    invented is a domain the run can never leave — and a phrase that is not a
    domain (`"official company websites"`) silently matches nothing.

    Measured: a run asked for "European logistics companies with their chief
    executive and their headquarters city"; the planner returned
    `["wikipedia.org", "official company websites", "dnb.com", "statista.com"]`.
    The primary search was pinned to those three real domains, returned nothing
    crawlable, and *every* source the run used came from an unrestricted
    fallback path. The planner was answering a question nobody asked, which is
    the same defect `_cap_total_fields` exists to stop — a schema nobody
    requested is not the schema the user wanted.
    """
    text = (prompt or "").lower()
    kept: list[str] = []
    for entry in entries or []:
        raw = str(entry or "").strip()
        if not raw:
            continue
        if "://" in raw:
            host = (urlparse(raw).hostname or raw).lower()
        else:
            host = raw.lower()
        host = host.strip(".")
        # A bare host must actually look like a host. "official company websites"
        # is a phrase; it belongs in neither filter.
        if " " in host or "." not in host:
            continue
        domain = ".".join(host.split(".")[-2:])
        if host in text or domain in text:
            kept.append(raw)
    return kept


def _finalize(plan: dict, prompt: str) -> dict:
    """The deterministic post-processing every rung shares.

    Kept in one place so the invariants cannot hold on one rung and not another:
    an allowlist only for requested domains, a passable required set, a bounded
    field count. Applied to the LLM rungs and the rule-based rung alike.
    """
    plan["allowed_sources"] = _requested_allowed_sources(
        prompt, plan.get("allowed_sources") or [])
    plan = _cap_total_fields(_cap_required(plan))
    plan = _entity_traversal(plan)
    return _merge_field_probes(plan)


async def compile_plan(prompt: str, llm: object = None) -> tuple[dict, str]:
    """Returns (plan_dict, provider). Raises E_VALIDATION on empty prompt.

    Rung order is: the injected `llm`, then the hardwired Gemini instructor, then
    the rule-based compiler. The injected provider comes first because it is the
    one the caller configured for this deployment — spending cloud quota on a
    different provider before trying it spends money to produce a plan the caller
    then has to pay a second model for anyway.

    `OPENCODE_STRICT` removes the Gemini rung entirely. That is the setting's
    whole purpose: a free local reader must not be able to quietly fall through
    to a billed cloud model, so under strict mode the only model rung is the
    injected `llm`.
    """
    if not (prompt or "").strip():
        raise validation("Prompt must not be empty")

    # Rung 1: the injected provider.
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
            return (_finalize(plan, prompt),
                (out.get("provider", "llm")
                 if isinstance(out, dict) else "llm"))
        except Exception:
            pass  # fall through to the next rung (honest, marked)

    # Rung 2: the hardwired Gemini instructor. Skipped entirely under strict mode.
    if not settings.OPENCODE_STRICT:
        hit = await _instructor_plan(prompt)
        if hit is not None:
            return hit

    # Rung 3: rule-based, always available, no model involved.
    try:
        return (_finalize(
            WorkflowPlan(**_rule_plan(prompt)).model_dump(), prompt), "rule-based")
    except Exception:
        return _fallback_plan(prompt), "fallback"


def coerce_plan(data: dict) -> WorkflowPlan:
    try:
        return WorkflowPlan(**data)
    except Exception as e:
        raise validation(f"Invalid WorkflowPlan: {e}")
