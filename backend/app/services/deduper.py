"""Dedup: L1 exact-normalized -> L2 RapidFuzz. Merge unions provenance.

L3 embeddings: Later adapter only (FEATURE_SEMANTIC_DEDUP + explicit provider).
MVP stops at L2 — documented, not omitted.

Complexity. This is the hot path of a run's finalisation: O(n²) signature
comparisons over every pair of records, plus a dict lookup per comparison. The
squared term is unavoidable for fuzzy matching (any pair may be a duplicate), but
it is paid on *strings*, and the previous shape made each comparison expensive:
`token_set_ratio` was called once per pair, and RapidFuzz is not cheap.

Two changes keep that affordable:

* `_Sig` holds both the pipe-joined key and a pre-tokenised tuple of the
  identity values. Comparing values rather than the joined signature is what
  makes L2 work at all (see `_similar`) and it removes the re-tokenisation that
  `token_set_ratio` did on every call.
* An exact-match `dict` keyed by signature short-circuits L1, so the common case
  — many records, few duplicates — touches `token_set_ratio` only for pairs that
  are not already known-identical.

`dedupe_records` returns *copies*. It used to mutate the caller's field dicts in
place while unioning provenance, and two other stages (`runner` and
`entity_gate`) hold and mutate those same dicts, so a merge could rewrite a
record another stage was in the middle of reading.
"""
import copy
import re

from rapidfuzz import fuzz


def _strip_parenthetical(value: str) -> str:
    """Drop a trailing parenthetical, keeping what remains.

    `Amazon Web Services (AWS)` is the same company as `Amazon Web Services`,
    and the parenthetical is a gloss on the name rather than part of it. Left
    in place it produces a signature no other spelling of the company can ever
    match, so the row is its own entity forever.

    Only the *trailing* parenthetical is removed. A parenthetical in the middle
    of a name usually carries real distinguishing information
    (`Acme (Europe) Ltd` vs `Acme (US) Ltd`), and dropping it would merge two
    genuinely different entities -- exactly the failure this module's corporate
    form handling exists to prevent.

    Refuses to strip a name that is *only* a parenthetical. `(unknown)` would
    otherwise normalise to the empty string, and an empty identity is not a
    useful thing to compare records on -- it makes every such row a candidate
    for merging with every other one.
    """
    inner = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    return inner or value.strip()


def _acronym_of(text: str) -> str:
    """The initials of a multi-word name, or "" if it cannot be one.

    Used to tie a bare acronym to the full name it stands for: "aws" is the
    initials of "amazon web services". Only computed for genuine multi-word
    names, and only consulted when the mapping is unambiguous within the run --
    an acronym shared by two different names proves nothing and is ignored.
    """
    words = [w for w in _identity(text).split() if len(w) > 1]
    if len(words) < 2:
        return ""
    return "".join(w[0] for w in words)


def _identity(value: object) -> str:
    """Normalise one identity value WITHOUT discarding its corporate form.

    Deliberately not `validator.normalize_key`, which strips `inc|llc|corp|ltd|
    limited|co|pvt|private`. That is right for comparing *references* to each
    other and catastrophic here: dedupe built its signature from it, so "Acme Inc"
    and "Acme LLC" both normalised to `acme` and matched on the **exact** tier.
    Two different legal entities were merged as byte-identical, with one row's
    figures unioned into the other and no conflict ever recorded.

    What is removed here is only presentation: case, accents, and punctuation.
    Word boundaries are preserved, because `Acme` and `Acme Holdings` have to stay
    distinguishable after all.
    """
    import unicodedata

    if value is None:
        return ""
    # Parenthetical first, while the brackets still exist: the punctuation
    # normalisation below erases them, turning `Amazon Web Services (AWS)` into
    # `amazon web services aws`, which is a name no other spelling can match.
    text = _strip_parenthetical(str(value))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


class _Sig:
    """One record's identity: the joined key plus its individual values.

    Built once per record instead of being re-derived for every comparison.

    The prefilter fields are derived from the *normalised stems* (after corporate
    form and decorations are removed), not the raw values, because that is the
    text `_similar` actually scores. Measuring the raw string made "acme" and
    "acme holdings" look like a 4-vs-13 character mismatch and rejected a genuine
    duplicate before it was ever compared.
    """

    __slots__ = ("joined", "values", "stems", "forms", "_len", "_chars", "_hash")

    def __init__(self, values: list[str]) -> None:
        self.values = values
        self.joined = "|".join(values)
        stems: list[str] = []
        forms: list[str] = []
        for value in values:
            if not value:
                continue
            stem, form = _split_legal(value)
            stems.append(stem)
            forms.append(form)
        self.stems = stems
        self.forms = forms
        self._len = max((len(s) for s in stems), default=0)
        self._chars = frozenset("".join(stems))
        # Hashable and comparable, because `backfill`, `gapfill` and `refresh`
        # use the signature as a dict key to find a matching stored record and
        # test its truthiness. A default-identity object would satisfy neither:
        # every record would share one bucket.
        self._hash = hash(self.joined)

    def __bool__(self) -> bool:
        """True when at least one key carried a value.

        An all-empty signature is an identity-free record: it must never merge,
        exact or fuzzy, or every empty row in a run collapses into one.
        """
        return any(v for v in self.values)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Sig):
            return NotImplemented
        return self.joined == other.joined

    def __str__(self) -> str:
        return self.joined

    def __repr__(self) -> str:
        return f"_Sig({self.joined!r})"

    def viable(self, other: "_Sig") -> bool:
        """False when the pair provably cannot reach a high score.

        Sound, not heuristic: both conditions are necessary for `token_set_ratio`
        over the stems to be large, so this never rejects a true duplicate.

        * character overlap — two names sharing almost no letters are not the
          same company however they are spelled;
        * length — the ratio caps the achievable score regardless of alignment.
        """
        if not self._chars or not other._chars:
            return False
        if len(self._chars & other._chars) < 3:
            return False
        lo, hi = min(self._len, other._len), max(self._len, other._len)
        return not hi or lo / hi >= 0.55


#: Blocking key widths. A pair is only compared if they share one of these keys,
#: which turns finalisation from O(n^2) comparisons into roughly O(n).
#:
#: The recall cost is real and worth stating: two names that are near-identical
#: but share none of these keys are not compared and would stay separate. A
#: third key on character length was tried and removed — it made things *worse*,
#: because company names cluster tightly on length, so "l11" collected a large
#: fraction of every run into one bucket and the blocking bought nothing. Prefix
#: and suffix are chosen because a genuine duplicate differs, if at all, in the
#: middle of the name.
_BLOCK_PREFIX = 3
_BLOCK_SUFFIX = 4


def _block_keys(sig: _Sig) -> tuple[str, ...]:
    """Blocking keys for one identity. Empty for an identity-free record.

    Built from the normalised stems, so "Acme" and "Acme Holdings" block together
    (same stem) exactly as they should score together.

    The suffix key skips the last word when that word is the corporate form. A
    company list is mostly `... Inc` / `... Ltd`, so keying on the literal tail
    put every record in the run into one bucket and the blocking bought nothing —
    measured at over five minutes for 5,000 rows before this was excluded.
    """
    joined = "|".join(sig.stems)
    if not joined or not sig:
        return ()
    keys = ["p" + joined[:_BLOCK_PREFIX]]
    # Drop a trailing corporate form from the suffix key only.
    tail = joined
    if sig.forms and sig.forms[-1]:
        for stem in reversed(sig.stems):
            if stem:
                tail = stem
                break
    if len(tail) > 1:
        keys.append("s" + tail[-_BLOCK_SUFFIX:])
    return tuple(keys)


def _sig(fields: dict, keys: list[str]) -> _Sig:
    """The identity signature of a record. Historical name, kept.

    `backfill`, `gapfill` and `refresh` import this and use the result as a dict
    key (to find the stored record that matches a freshly extracted one) and as a
    truthiness test (an identity-free record must not match anything). `_Sig` is
    hashable and defines `__bool__` for exactly that reason.

    Returns a `_Sig`, not a `str`. Those callers only ever used it as an opaque
    key and as a boolean, and `dedupe_records` no longer compares signatures as
    strings — it compares normalised stems — so returning the object is safe and
    keeps the fuzzy tier's precomputation available to callers that want it.
    """
    used = keys or sorted(fields)
    return _Sig([_identity(fields.get(k)) for k in used])


def _signature(fields: dict, keys: list[str]) -> _Sig:
    return _sig(fields, keys)


#: Field-name hints that carry identity. The planner usually emits a real
#: `dedupe_keys`, but when it emits none the old fallback used *every* field —
#: so two rows were identical only when everything matched, and near-duplicates
#: that differed in one scraped decoration never merged.
_IDENTITY_FIELD_HINTS = ("name", "title", "company", "organization", "organisation",
                          "entity", "person", "event", "product", "school",
                          "hospital", "startup", "website", "url", "link")


def _fallback_keys(field_names: list[str]) -> list[str]:
    """Identity-like fields, or every field when none look like identity.

    Narrow on purpose: matching on all fields conflates identity with
    completeness, so a row missing one decoration could never match its twin.
    When nothing looks like identity the old behavior is kept rather than
    invented — an unknown shape must not gain a confident new merge rule.
    """
    names = [str(n) for n in field_names or []]
    hits = [n for n in names
            if n.lower() in _IDENTITY_FIELD_HINTS
            or n.lower().endswith(("_name", "_title", "_url", "_website", "_link"))
            or n.lower() in ("id", "slug", "handle")]
    return sorted(hits) or sorted(names)


#: Corporate forms, mapped to a canonical token. Two forms match only if their
#: canonical tokens agree, so "Acme Corp." and "Acme Corporation" are one company
#: while "Acme Inc" and "Acme LLC" are not.
_LEGAL_FORMS: dict[str, str] = {
    "inc": "inc", "incorporated": "inc",
    "llc": "llc", "llp": "llc",
    "ltd": "ltd", "limited": "ltd",
    "corp": "corp", "corporation": "corp", "co": "corp",
    "plc": "plc", "gmbh": "gmbh", "ag": "ag",
    "sa": "sa", "sarl": "sa", "bv": "bv", "b v": "bv",
    "pty": "pty", "pvt": "pty", "private": "pty",
    "srl": "srl", "oy": "oy", "ab": "ab", "as": "as", "aps": "aps",
    "holdings": "", "holding": "", "group": "", "the": "",
}


def _split_legal(value: str) -> tuple[str, str]:
    """`"acme inc"` -> `("acme", "inc")`. Stem and canonical corporate form.

    The form has to be split out rather than stripped, because it is the one token
    that must *not* be forgiven. "Acme Inc" and "Acme LLC" share a stem and differ
    only in corporate form; merging them silently folds one company's rows — and
    their differing figures — into the other.

    Forms are canonicalised so an abbreviation does not read as a difference:
    `corp` and `corporation` are the same form, `inc` and `incorporated` are the
    same form. Decorations like "holdings" are dropped from the stem entirely
    rather than treated as a form, since "Acme" and "Acme Holdings" are still the
    same company.

    A form is recognised wherever it appears, not only as the last word:
    "Hewlett Packard Enterprise Inc" and "Initech Software GmbH" both carry one
    mid-string. Treating only the final word as the form left "inc" inside the
    stem, where it read as a distinguishing token.
    """
    parts = [p for p in value.split() if p]
    if not parts:
        return "", ""

    form = ""
    kept: list[str] = []
    for part in parts:
        canonical = _LEGAL_FORMS.get(part)
        if canonical is None:
            kept.append(part)
        elif not form and canonical:
            form = canonical
    # Words mapping to "" (holdings, group, the) are decorations and vanish.
    stem = " ".join(kept).strip()
    return stem, form


def _similar(a: _Sig, b: _Sig, threshold: float) -> float:
    """Best similarity between two identities.

    Compared on the *values*, not on `joined`. `token_set_ratio` splits its input
    on whitespace and a pipe-joined signature has no whitespace, so
    `"acme|berlin"` and `"acmecorp|berlin"` were each a single token and scored
    ~50 against a threshold of 88. The fuzzy tier therefore never fired for any
    single-key schema, which is most of them.

    A multi-key identity needs every key to agree, so the weakest pair decides:
    two companies sharing a city are not the same company.

    Per value:

    * a missing value on either side is never a match — an unknown field cannot
      vouch for a duplicate;
    * two *different* declared corporate forms are never a match;
    * otherwise `token_set_ratio` decides, with `partial_ratio` as a second
      opinion on long names only. RapidFuzz matches any substring, so on a short
      name "Acme" would match "Acme Holdings Worldwide"; on a long one it is
      usually a typo.
    """
    best = 100.0
    for sx, sy, form_x, form_y in zip(a.stems, b.stems, a.forms, b.forms):
        if not sx or not sy:
            return 0.0
        if form_x and form_y and form_x != form_y:
            return 0.0
        # Different word counts mean different things, not a typo. RapidFuzz is
        # happy to call "acme" and "acme holdings worldwide" a match because one
        # contains the other; it also means the shorter is a truncation, and a
        # truncation of a company name is far more often a different entity than
        # the same one written badly. Requiring equal length keeps the fuzzy tier
        # for genuine spelling variants ("Börse"/"Borse") rather than for
        # substring containment.
        if len(sx.split()) != len(sy.split()):
            return 0.0
        if sx == sy:
            continue
        # `fuzz.ratio`, not `fuzz.token_set_ratio`.
        #
        # token_set_ratio discards the tokens a pair does NOT share and scores
        # only the common subset, which is exactly wrong for entity identity:
        # "Acme Technologies Inc abc" and "Acme Technologies Inc xyz" share
        # three of four tokens and scored 91 — above the threshold — on the
        # strength of a prefix that says nothing about which company this is.
        # Plain ratio compares the whole normalised string, so a token that
        # differs costs what it should.
        ratio = fuzz.ratio(sx, sy)
        if ratio < best:
            best = ratio
        if best < 60.0:
            return best
    return best


def dedupe_records(rows: list[dict], keys: list[str],
                   threshold: float = 88.0) -> tuple[list[dict], int]:
    """Rows: {fields: {name: provenance-field}}. Returns (canonical, merged_count).

    Identity rule: a row whose dedupe signature is EMPTY (all key values missing)
    carries no identity evidence and never merges — neither exact nor fuzzy.
    Two empty signatures would otherwise false-positive on every run.

    Returns fresh dicts; the input rows and their field dicts are not modified.
    """
    from app.core.config import settings as _settings
    if _settings.FEATURE_SEMANTIC_DEDUP:
        raise RuntimeError(
            "FEATURE_SEMANTIC_DEDUP is a Phase-2 gate with no embedding provider wired. "
            "Refusing before any merge work (fail fast, no partial state). "
            "Disable the flag to use L1/L2.")

    canon_sigs: list[_Sig] = []
    canon_rows: list[dict] = []
    # Exact-signature -> index into canon_*. Turns L1 from a linear scan into a
    # hash lookup, which is the difference between O(n²) and O(n) for the common
    # case of a run whose records are mostly distinct.
    exact: dict[str, int] = {}
    # Blocking: block key -> indices whose signatures share it. A candidate pair
    # is only ever drawn from these buckets.
    buckets: dict[str, list[int]] = {}
    merged = 0

    for row in rows:
        plain = {k: (v.get("value") if isinstance(v, dict) else v)
                 for k, v in row.get("fields", {}).items()}
        # `keys` are the plan's declared identity. The empty-plan fallback lives
        # here — not in `_sig` — because backfill/gapfill/refresh deliberately
        # refuse to match without declared keys, and narrowing the shared helper
        # would silently permit the matches those paths refuse to make.
        sig = _signature(plain, keys or _fallback_keys(list(plain)))

        idx = exact.get(sig.joined, -1) if sig else -1
        if idx < 0 and sig:
            candidates: set[int] = set()
            for key in _block_keys(sig):
                candidates.update(buckets.get(key, ()))
            for i in candidates:
                c = canon_sigs[i]
                if not c.viable(sig):
                    continue
                if _similar(c, sig, threshold) >= threshold:
                    idx = i
                    break

        if idx < 0:
            canon_sigs.append(sig)
            canon_rows.append(copy.deepcopy(row))
            if sig:
                pos = len(canon_rows) - 1
                exact.setdefault(sig.joined, pos)
                for key in _block_keys(sig):
                    buckets.setdefault(key, []).append(pos)
        else:
            merged += 1
            target = canon_rows[idx].setdefault("fields", {})
            for k, v in row.get("fields", {}).items():
                cur = target.get(k, {})
                if not isinstance(cur, dict) or cur.get("value") in (None, ""):
                    target[k] = copy.deepcopy(v)
                elif (isinstance(v, dict)
                      and v.get("value") not in (None, "")
                      and cur.get("value") != v.get("value")):
                    # Genuine conflict on merge: keep first value, mark conflicting,
                    # preserve the rival for the JUDGE step (Jev-C).
                    #
                    # The rival used to be stored as `{value, source}` and
                    # nothing else, so when Jev-C adopted it there was no status
                    # to inherit and the code had nothing to copy but a hardcoded
                    # "verified". That is why a decision about *which value is
                    # better* ended up recorded as *the evidence is sound* — an
                    # invariant that could not be honoured because the evidence
                    # had not been carried along.
                    #
                    # So the rival now carries its own status and locator, and
                    # adoption inherits them instead of inventing them.
                    cur["verification_status"] = "conflicting"
                    cur.setdefault("rivals", []).append({
                        "value": v.get("value"),
                        "source": copy.deepcopy(v.get("source", {})),
                        "verification_status": str(
                            (v.get("verification_status") or "") or ""),
                    })
    return canon_rows, merged


class CrossDatasetRefused(Exception):
    """Detection cannot run safely. Reported, never partially applied."""


def find_cross_dataset_duplicates(own_records: list[dict], keys: list[str],
                                  others: list[tuple[str, list[dict]]]) -> dict:
    """Records in *other* datasets carrying this dataset's identities.

    Detection only — nothing is merged, moved, or rewritten. Merging across
    datasets would fold one user's question into another's, and two datasets
    built from different plans can legitimately hold the same entity for
    different reasons. What this answers is the audit question: "does this
    entity already exist elsewhere in the library, and where?"

    `keys` must be the plan's declared `dedupe_keys`, recovered the same way
    the run used them. Without declared keys there is no safe identity to
    match on — the same refusal backfill, gapfill and refresh make — so this
    raises rather than falling back to all-fields matching, which would report
    a duplicate only when every field agrees.
    """
    if not keys:
        raise CrossDatasetRefused(
            "this dataset's plan declares no dedupe_keys, so records in other "
            "datasets cannot be matched to an identity; matching on every "
            "field would report a duplicate only when everything agrees, "
            "which is a different question with a confident wrong answer")
    own_sigs: dict[str, dict] = {}
    for rec in own_records or []:
        plain = {k: (v.get("value") if isinstance(v, dict) else v)
                 for k, v in (rec.get("fields") or {}).items()}
        sig = _signature(plain, keys)
        if sig:
            own_sigs.setdefault(sig.joined, rec)
    if not own_sigs:
        return {"compared_datasets": len(others), "matches": 0,
                "matched_datasets": [], "examples": [],
                "note": "none of this dataset's records carries an identity, "
                        "so there is nothing to look for elsewhere"}
    matched: dict[str, list[dict]] = {}
    for dataset_id, records in others or []:
        for rec in records or []:
            plain = {k: (v.get("value") if isinstance(v, dict) else v)
                     for k, v in (rec.get("fields") or {}).items()}
            sig = _signature(plain, keys)
            if sig and sig.joined in own_sigs:
                matched.setdefault(str(dataset_id), []).append({
                    "identity": sig.joined,
                    "fields": {k: plain.get(k) for k in keys if plain.get(k)}})
    matched_datasets = sorted(matched)
    examples = []
    for dataset_id in matched_datasets[:5]:
        for hit in matched[dataset_id][:3]:
            examples.append({"dataset_id": dataset_id, **hit})
    return {"compared_datasets": len(others),
            "matches": sum(len(v) for v in matched.values()),
            "matched_datasets": matched_datasets,
            "examples": examples,
            "note": ("detection only: nothing was merged, moved, or rewritten"
                     if matched_datasets else
                     "no record in the compared datasets carries one of "
                     "this dataset's identities")}


async def adjudicate_conflicts(rows: list[dict]) -> dict:
    """Explicit JUDGE step (Jev-C): confirm/adopt/downgrade conflicting cells.

    A: keep incumbent. B: adopt rival value+source. CONFLICT: keep conflicting.
    INSUFFICIENT: downgrade to unverified (honest, never guessed).
    Returns {judged, confirmed, adopted, downgraded}.

    The cells are judged concurrently. One live run merged 35 duplicates and
    then spent the remainder of its 600s budget adjudicating their conflicts
    one at a time, which is what pushed the run past its limit; the cells are
    independent, so the wall cost is now the slowest call rather than the sum.
    """
    import asyncio
    from app.providers.decision import jev
    from app.core.config import settings
    from app.services import validator as validator_svc

    stats = {"judged": 0, "confirmed": 0, "adopted": 0, "downgraded": 0}
    sem = asyncio.Semaphore(max(1, int(getattr(settings, "ITEM_CONCURRENCY", 8) or 8)))

    async def _judge(name: str, cell: dict, rival: dict) -> str:
        async with sem:
            res = await jev.conflict_triage(
                name, str(cell.get("value", "")), str(rival.get("value", "")),
                (cell.get("source", {}) or {}).get("quote", ""),
                (rival.get("source", {}) or {}).get("quote", ""))
        return str(res.get("decision", "CONFLICT"))

    jobs: list[tuple[str, dict, dict]] = []
    for row in rows:
        for name, cell in row.get("fields", {}).items():
            if not isinstance(cell, dict) or cell.get("verification_status") != "conflicting":
                continue
            rivals = cell.get("rivals", []) or []
            if not rivals:
                continue
            jobs.append((name, cell, rivals[0]))

    stats["judged"] = len(jobs)
    if not jobs:
        return stats
    verdicts = await asyncio.gather(
        *(_judge(n, c, rv) for n, c, rv in jobs), return_exceptions=True)
    for (_name, cell, rival), item in zip(jobs, verdicts):
        if isinstance(item, BaseException):
            # A failed judge leaves the cell conflicting, which is the safe
            # outcome: a conflict is never silently resolved one way.
            stats["confirmed"] += 1
            continue
        if item == "B":
            # Triage answered "which of these two values is better". It did not
            # check that the rival's quote resolves against its own stored page,
            # and it has no support verdict for it. So the adopted cell inherits
            # the rival's status and locator, and `adopt` demotes anything that
            # claims `verified` without offsets behind it.
            #
            # `update` rather than rebinding: the caller holds this dict by
            # reference out of the row it is about to be persisted from.
            cell.update(validator_svc.adopt(cell, rival, resolved="adopted_rival"))
            stats["adopted"] += 1
        elif item == "INSUFFICIENT":
            cell["value"] = None
            cell["verification_status"] = "unverified"
            stats["downgraded"] += 1
        else:
            stats["confirmed"] += 1  # A or CONFLICT: incumbent stands as conflicting
    return stats
