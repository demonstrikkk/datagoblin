"""Phase-4 deterministic-first extraction: LLM once, deterministic forever.

For repeat domains (listings, tables, catalogs) an LLM read of every page is
wasteful AND less trustworthy than a selector. This module implements the
deterministic rung that runs BEFORE the LLM in extract_page:

1. Cached domain schema (backend/selectors/<domain>.json, checked in): ordered
   CSS selector alternates per field. First matching alternate wins; the
   evidence quote is the element text itself — verbatim by construction.
2. Regex extractors for pattern fields (email/phone/price/url), gated on
   field-name hints. The match string IS the quote.
3. Adaptive relocate (Scrapling pattern): when every stored selector misses
   but the schema carries a structural snapshot, rescore candidates with
   difflib.SequenceMatcher and take the best above threshold. Pure parsing
   resilience — no stealth surface. Top candidates are returned for logging.

A deterministic win with coverage "full" (all required fields) returns
immediately with provider "selectors:<domain>"; anything less falls through
to the LLM. The LLM stays the fallback, never the default, for regular pages.

Schema generation ("pay once") lives in build_schema_prompt() + the
gen_selectors script: an LLM proposes selectors from one sample page, a human
reviews, the JSON is checked in. Runtime never writes schema files.
"""
import difflib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RELOCATE_THRESHOLD = 0.4

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_PRICE_RE = re.compile(r"\$\s?[\d,]+(?:\.\d{1,2})?\s?(?:million|billion|thousand|[kmb])?",
                       re.IGNORECASE)
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")

# field-name hints -> (pattern name, compiled regex). Conservative: a pattern
# only fires when the field name asks for it, so years never become phones.
_REGEX_EXTRACTORS: tuple[tuple[frozenset, str, Any], ...] = (
    (frozenset({"email", "contact"}), "email", _EMAIL_RE),
    (frozenset({"phone", "tel", "contact"}), "phone", _PHONE_RE),
    (frozenset({"price", "salary", "funding", "amount", "cost", "valuation",
                "compensation", "pay"}), "price", _PRICE_RE),
    (frozenset({"url", "website", "link", "homepage", "site"}), "url", _URL_RE),
)


def selectors_dir() -> Path:
    """Directory of checked-in domain schemas (backend/selectors by default)."""
    from app.core.config import settings
    configured = (settings.SELECTORS_DIR or "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        return path
    return Path(__file__).resolve().parents[2] / "selectors"


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _schema_path(domain: str) -> Path:
    safe = re.sub(r"[^a-z0-9.-]", "_", (domain or "").lower())[:100]
    return selectors_dir() / f"{safe}.json"


def load_schema(domain: str) -> dict | None:
    """Load and shape-validate a domain schema. None when absent/invalid.

    Shape: {"domain": str, "fields": {name: {"selectors": [css, ...],
    "snapshot": {...}}}, ...}. Invalid files fail closed (None, never raise).
    """
    if not domain:
        return None
    try:
        data = json.loads(_schema_path(domain).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
        return None
    fields: dict[str, dict] = {}
    for name, spec in data["fields"].items():
        if not isinstance(spec, dict):
            continue
        selectors = [s for s in (spec.get("selectors") or []) if isinstance(s, str)]
        if not selectors:
            continue
        snapshot = spec.get("snapshot") if isinstance(spec.get("snapshot"), dict) else {}
        fields[str(name)] = {"selectors": selectors[:5], "snapshot": snapshot,
                             "multiple": bool(spec.get("multiple", False))}
    if not fields:
        return None
    item = data.get("item") if isinstance(data.get("item"), str) else ""
    return {"domain": domain, "fields": fields, "item": item,
            "generated_by": str(data.get("generated_by", "")),
            "generated_at": str(data.get("generated_at", ""))}


def _element_text(el: Any) -> str:
    try:
        return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    except Exception:
        return ""


def page_text(html: str) -> str:
    """Visible page text for gating selector quotes (same decompose as fetch)."""
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for t in soup(["script", "style", "nav", "footer", "svg", "noscript", "iframe"]):
            t.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    except Exception:
        return ""


def css_extract(html: str, selectors: list[str]) -> tuple[str, str]:
    """First matching alternate wins. Returns (text, selector) or ("", "")."""
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return "", ""
    texts, selector = _field_candidates(soup, selectors)
    if not texts:
        return "", ""
    return texts[0], selector


def _field_candidates(root: Any, selectors: list[str]) -> tuple[list[str], str]:
    """All non-empty texts of the first hitting alternate. Returns (texts, selector)."""
    for selector in selectors:
        try:
            els = root.select(selector)
        except Exception:
            continue  # invalid selector: next alternate, never raise
        texts = [_element_text(e) for e in (els or [])]
        texts = [t for t in texts if t]
        if texts:
            return texts, selector
    return [], ""


def _snapshot_element(el: Any) -> dict:
    try:
        attrs = dict(getattr(el, "attrs", {}) or {})
    except Exception:
        attrs = {}
    flat = {k: (" ".join(v) if isinstance(v, list) else str(v)) for k, v in attrs.items()}
    try:
        parent = el.parent
        parent_tag = getattr(parent, "name", "") or ""
    except Exception:
        parent_tag = ""
    return {"tag": getattr(el, "name", "") or "", "attrs": flat,
            "text": _element_text(el)[:200], "parent_tag": parent_tag}


def _similarity(snapshot: dict, el: Any) -> float:
    """0..1 structural similarity between a stored snapshot and a candidate."""
    cand = _snapshot_element(el)
    scores: list[float] = []
    if snapshot.get("tag") or cand["tag"]:
        scores.append(1.0 if snapshot.get("tag") == cand["tag"] else 0.0)
    if snapshot.get("parent_tag") or cand["parent_tag"]:
        scores.append(1.0 if snapshot.get("parent_tag") == cand["parent_tag"] else 0.0)
    old_text, new_text = snapshot.get("text", ""), cand["text"]
    if old_text or new_text:
        scores.append(difflib.SequenceMatcher(None, old_text, new_text).ratio())
    old_attrs = snapshot.get("attrs", {}) or {}
    if old_attrs or cand["attrs"]:
        keys = set(old_attrs) | set(cand["attrs"])
        per_key = [difflib.SequenceMatcher(
            None, str(old_attrs.get(k, "")), str(cand["attrs"].get(k, ""))).ratio()
            for k in keys]
        scores.append(sum(per_key) / len(per_key))
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 4)


def relocate(html: str, snapshot: dict,
             threshold: float = RELOCATE_THRESHOLD) -> tuple[str, str, list]:
    """Rescore candidates against a structural snapshot after a selector miss.

    Returns (text, why, top5) where top5 is [{score, tag, text}] for logging.
    ("", "", top5) when nothing clears the threshold. Depth-scoped scan is
    unnecessary at our page sizes; a bounded full-tree scan suffices.
    """
    from bs4 import BeautifulSoup
    if not snapshot:
        return "", "", []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return "", "", []
    scored: list[tuple[float, Any]] = []
    # Bounded scan (first 2000 elements): typical pages are far smaller; the
    # cap keeps pathological pages from stalling the deterministic rung.
    for el in soup.find_all(True)[:2000]:
        try:
            score = _similarity(snapshot, el)
        except Exception:
            continue
        scored.append((score, el))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = [{"score": s, "tag": getattr(el, "name", ""),
            "text": _element_text(el)[:120]} for s, el in scored[:5]]
    if scored and scored[0][0] >= threshold:
        best = scored[0][1]
        return _element_text(best), "relocated", top
    return "", "", top


def regex_extract(field_name: str, text: str) -> tuple[str, str]:
    """Pattern-field extraction gated on field-name hints. Returns (match, kind)."""
    name = (field_name or "").lower()
    for hints, kind, pattern in _REGEX_EXTRACTORS:
        if not any(h in name for h in hints):
            continue
        try:
            m = pattern.search(text or "")
        except Exception:
            continue
        if m:
            return m.group(0).strip(), kind
    return "", ""


def deterministic_extract(plan: dict, page: dict) -> tuple[list[dict], str, dict]:
    """Try selectors, then regex, per plan field. Returns (records, coverage, debug).

    Two shapes: without schema "item", one record from page-level matches;
    with "item" (listing pages), one record per container (capped at 25),
    fields scoped inside their container. A field with "multiple": true joins
    all its matches with " | ". Regex pattern-fields apply per record text.
    debug carries per-field {method, selector/kind} plus relocate top-5.
    Empty records + "none" when nothing fired.
    """
    from bs4 import BeautifulSoup
    fields_spec = plan.get("fields", []) or []
    # Rendered snapshot doubles as DOM source: browser pages carry
    # rendered_html instead of (static) html — selectors run on either.
    html = page.get("html", "") or page.get("rendered_html", "") or ""
    if not fields_spec:
        return [], "none", {}
    url = page.get("url", "")
    schema = load_schema(domain_of(url))
    schema_fields = (schema or {}).get("fields", {}) if schema else {}
    item_selector = (schema or {}).get("item", "") if schema else {}
    clean = page.get("markdown") or ""
    if not clean and html:
        from app.services.reducer import reduce_html
        clean = reduce_html(html)
    if not clean and not html:
        return [], "none", {}

    soup = None
    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            soup = None
    containers: list = [None]
    if item_selector and soup is not None:
        try:
            containers = soup.select(item_selector)[:25] or [None]
        except Exception:
            containers = [None]

    required = [f.get("name") for f in fields_spec
                if isinstance(f, dict) and f.get("required")]
    records: list[dict] = []
    debug: dict[str, dict] = {}
    for container in containers:
        scope = container if container is not None else soup
        scope_text = _element_text(container) if container is not None else clean
        out_fields: dict[str, Any] = {}
        out_evidence: list[dict] = []
        for f in fields_spec:
            name = f.get("name", "") if isinstance(f, dict) else ""
            if not name:
                continue
            value, quote, method = "", "", ""
            spec = schema_fields.get(name, {})
            if spec and scope is not None:
                texts, selector = _field_candidates(scope, spec["selectors"])
                if texts:
                    value = " | ".join(texts) if spec.get("multiple") else texts[0]
                    quote, method = value, f"css:{selector}"
                elif spec.get("snapshot") and container is None and html:
                    value, why, top = relocate(html, spec["snapshot"])
                    if value:
                        quote, method = value, why
                    debug[name] = {"method": method or "miss", "top": top}
                else:
                    debug[name] = {"method": "miss", "top": []}
            if not value:
                value, kind = regex_extract(name, scope_text)
                if value:
                    quote, method = value, f"regex:{kind}"
                    debug.setdefault(name, {"method": method, "top": []})
            if value:
                out_fields[name] = value
                out_evidence.append({"field": name, "value": value, "quote": quote,
                                     "source_url": url, "reference_id": ""})
                debug.setdefault(name, {"method": method, "top": []})
        if out_fields:
            coverage = "full" if all(r in out_fields for r in required) else "partial"
            records.append({"fields": out_fields, "evidence": out_evidence,
                            "source_url": url, "coverage": coverage})
    if not records:
        return [], "none", debug
    overall = "full" if all(r["coverage"] == "full" for r in records) else "partial"
    return records, overall, debug


def build_schema_prompt(fields_spec: list[dict], html: str, url: str) -> str:
    """One-shot prompt: propose 2-3 CSS selector alternates per field.

    Used by the gen_selectors script (human reviews before check-in). The
    sample HTML is truncated — selector design needs structure, not content.
    """
    from app.schemas.evidence import simplify_schema
    schema = simplify_schema(fields_spec)
    lines = [f"{n} ({s['type']})" for n, s in schema.items()]
    sample = (html or "")[:8000]
    return (
        "Propose CSS selectors that extract each field from pages with this "
        "structure. Return JSON {\"fields\": {\"<name>\": {\"selectors\": "
        "[\"<css>\", \"<css-alt>\"], \"snapshot\": {\"tag\": \"<tag>\", "
        "\"attrs\": {\"class\": \"...\"}, \"text\": \"<sample>\", "
        "\"parent_tag\": \"<tag>\"}}}}. No prose. Prefer stable hooks (ids, "
        "data-* attributes) first, then semantic tags, then class chains; "
        "avoid nth-of-type unless structural. 2-3 alternates per field, most "
        "specific first.\n"
        f"Fields:\n" + "\n".join(lines) +
        f"\nURL: {url}\nHTML sample:\n{sample}")
