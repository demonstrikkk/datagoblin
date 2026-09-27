"""HTML -> clean text, capped. Ladder of real rungs (first non-empty wins):

trafilatura-markdown -> bs4-text -> cleanup-html -> html2text-markdown
-> script-json (Next.js __NEXT_DATA__-style payloads the DOM transform drops).

reduce_html keeps its (html, max_chars) -> text contract; the rung used is
available via reduce_html_with_rung for per-URL fetch-strategy logging.
Optional third-party rungs are import-guarded, never hard requirements.
"""
import json
import re

from app.core.config import settings

_SCRIPT_JSON = re.compile(
    r"(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*(\{.*?\});"
    r"|(?:window|document)\.[A-Za-z_$][\w$.]*\s*=\s*(\{.*?\});"
    r"|type=\"application/json\"[^>]*>(.*?)</script>"
    r"|id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>",
    re.DOTALL)


def _bs4_soup(html: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "nav", "footer", "svg", "noscript", "iframe"]):
        t.decompose()
    return soup


def _rung_trafilatura(html: str) -> str:
    from trafilatura import extract
    # include_tables=True: tables are first-class content (spec sheets, stats,
    # pricing grids) — the extractor must see them, not a redacted husk.
    return extract(html or "", output_format="markdown", with_metadata=False,
                   include_comments=False, include_tables=True) or ""


def _rung_bs4(html: str) -> str:
    return _bs4_soup(html).get_text(" ", strip=True)


def _rung_cleanup(html: str) -> str:
    """Title + de-scripted body text (ScrapeGraph ladder rung).

    Note: link absolutization needs the page URL, which this rung does not
    receive — hrefs are left as-is; this rung optimizes for text recovery.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    body = soup.body or soup
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
    return f"{title}\n{text}".strip()


def _rung_html2text(html: str) -> str:
    import html2text
    conv = html2text.HTML2Text()
    conv.ignore_links = False
    conv.body_width = 0
    return conv.handle(html or "")


def _rung_script_json(html: str) -> str:
    """Recover JSON payloads embedded in script tags (SPA data blobs, __NEXT_DATA__)."""
    parts: list[str] = []
    for m in _SCRIPT_JSON.finditer(html or ""):
        blob = next((g for g in m.groups() if g), "")
        blob = (blob or "").strip()
        if len(blob) < 20:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        flat = json.dumps(data, ensure_ascii=False)[:20000]
        # keep human-readable leaf strings alongside the raw blob
        leaves = re.findall(r'"(?:[^"\\]|\\.){4,200}"', flat)
        parts.append(" ".join(l.strip('"') for l in leaves[:400]))
    return " ".join(parts)


_RUNGS: tuple[tuple[str, object], ...] = (
    ("trafilatura", _rung_trafilatura),
    ("bs4", _rung_bs4),
    ("cleanup", _rung_cleanup),
    ("html2text", _rung_html2text),
    ("script-json", _rung_script_json),
)


def reduce_html_with_rung(html: str, max_chars: int = 0) -> tuple[str, str]:
    """Returns (text[:cap], rung_name). Rung is '' when every rung came up empty."""
    cap = max_chars or settings.REDUCE_MAX_CHARS
    for name, fn in _RUNGS:
        try:
            text = (fn(html) or "").strip()
        except Exception:
            continue  # missing optional dep or parse failure: next rung
        if text:
            return text[:cap], name
    return "", ""


def reduce_html(html: str, max_chars: int = 0) -> str:
    text, _ = reduce_html_with_rung(html, max_chars)
    return text


def media_block(page: dict, max_items: int = 30) -> str:
    """Grounded media appendix: image alts + media URLs (binaries never fetched).

    Moved here from extractor so the evidence store and the extractor can build
    the identical text. Alt text is quotable evidence exactly like body copy, so
    if the store omits it a quote lifted from an image cannot be re-verified.
    """
    images = page.get("images", []) or []
    videos = page.get("videos", []) or []
    if not images and not videos:
        return ""
    lines = ["", "Media on this page (URLs with alt text; binaries not fetched):"]
    for img in images[:max_items]:
        if isinstance(img, dict):
            alt, src = str(img.get("alt", "") or "").strip(), str(img.get("src", ""))
        else:
            alt, src = "", str(img)
        lines.append(f"- image: {alt} <{src}>" if alt else f"- image: <{src}>")
    for v in videos[:max_items]:
        lines.append(f"- video: <{str(v)}>")
    return "\n".join(lines)


def page_evidence_text(page: dict, max_chars: int = 0) -> str:
    """Every quotable character on a page, in one place.

    This is the single definition shared by the evidence store and the
    extractor. They used to disagree: the extractor appended media alt-text and
    JSON-LD to what it read, while the stored copy was built by a separate
    expression. Any difference between the two makes a verified quote
    unverifiable against the stored page, which defeats the point of storing it.

    Rendered rungs return `markdown` and no `html`; static rungs return `html`
    and no `markdown`. Both are folded in, so the stored text is a superset of
    whatever the LLM was shown.
    """
    from app.services import selectors as selectors_svc  # local: avoids a cycle
    dom_source = page.get("html", "") or page.get("rendered_html", "")
    parts: list[str] = []
    if dom_source:
        try:
            parts.append(selectors_svc.page_text(dom_source))
        except Exception:  # noqa: BLE001 (malformed markup is not a store failure)
            pass
    parts.append(page.get("markdown") or (reduce_html(dom_source) if dom_source else ""))
    if dom_source:
        try:
            structured = extract_structured(dom_source)
            if structured.get("text"):
                parts.append(str(structured["text"]))
        except Exception:  # noqa: BLE001
            pass
    block = media_block(page)
    if block:
        parts.append(block)
    out = "\n".join(p for p in parts if p).strip()
    return out[:max_chars] if max_chars and len(out) > max_chars else out


def _walk_json_ld(node: object, found: dict) -> None:
    """Collect Article-ish fields from JSON-LD (dicts, lists, @graph)."""
    if isinstance(node, list):
        for item in node:
            _walk_json_ld(item, found)
        return
    if not isinstance(node, dict):
        return
    if isinstance(node.get("@graph"), list):
        _walk_json_ld(node["@graph"], found)
    types = node.get("@type", [])
    types = [types] if isinstance(types, str) else (types or [])
    if any(t in ("Article", "NewsArticle", "BlogPosting", "WebPage",
                 "TechArticle", "ReportageNewsArticle") for t in types):
        for key in ("headline", "name", "description", "articleBody",
                    "datePublished", "dateModified", "keywords"):
            value = node.get(key)
            if value and key not in found:
                found[key] = value
        author = node.get("author")
        if author and "author" not in found:
            if isinstance(author, dict):
                found["author"] = author.get("name", "")
            elif isinstance(author, list):
                names = [a.get("name", "") for a in author if isinstance(a, dict)]
                found["author"] = ", ".join(n for n in names if n)
            else:
                found["author"] = str(author)
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_json_ld(value, found)


def extract_structured(html: str, max_chars: int = 4000) -> dict:
    """Author/date/tags/body from JSON-LD + meta tags. Pure function.

    Returns {author, published, description, tags, body, text} where text is
    the quotable appendix ("" when nothing found). Runs on EVERY page with
    HTML — SSR payloads carry the article even when the DOM transform drops
    it, and meta tags name authors/dates the body never mentions.
    """
    out = {"author": "", "published": "", "description": "", "tags": [],
           "body": "", "text": ""}
    if not html:
        return out
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return out
    found: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            _walk_json_ld(json.loads(script.string or ""), found)
        except Exception:
            continue
    meta_map = {"author": [("name", "author"), ("property", "article:author")],
                "published": [("property", "article:published_time"),
                              ("property", "article:modified_time"),
                              ("name", "date")],
                "description": [("name", "description"),
                                ("property", "og:description")]}
    meta: dict = {}
    for key, attrs in meta_map.items():
        for attr, val in attrs:
            tag = soup.find("meta", attrs={attr: val})
            if tag and (tag.get("content") or "").strip():
                meta[key] = tag["content"].strip()[:500]
                break
    kw = soup.find("meta", attrs={"name": "keywords"})
    tags = [t.strip() for t in str(kw.get("content", "")).split(",") if t.strip()][:20] \
        if kw else []
    author = str(found.get("author", "") or meta.get("author", ""))[:300]
    published = str(found.get("datePublished", "") or meta.get("published", ""))[:100]
    description = str(found.get("description", "") or meta.get("description", ""))[:500]
    body = str(found.get("articleBody", ""))[: max_chars]
    lines = []
    if author:
        lines.append(f"- author: {author}")
    if published:
        lines.append(f"- published: {published}")
    if tags:
        lines.append(f"- tags: {', '.join(tags)}")
    if description:
        lines.append(f"- description: {description}")
    if body:
        lines.append(f"- article body: {body}")
    out.update({"author": author, "published": published,
                "description": description, "tags": tags, "body": body,
                "text": ("Structured data (JSON-LD/meta):\n" + "\n".join(lines)
                         if lines else "")})
    return out
