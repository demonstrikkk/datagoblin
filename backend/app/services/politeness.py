"""Phase-3 politeness core: adaptive throttle, Retry-After, error strategy,
URL normalization + fingerprints, fetch statistics. Pure logic + tiny state.

Mechanisms (ported patterns, stdlib-only):
- AutoThrottle math (Scrapy): delay <- clamp(mean(delay, rtt/N)), asymmetric
  (trouble never speeds us up), Retry-After penalty, 60s cap. Applied ON TOP
  of the existing 1s/host floor in fetcher.throttle — never below it.
- classify strategy (Crawlee 3-budget spirit): fatal (policy/client — never
  retry) vs retry (backoff, same method) vs escalate (alternate method
  immediately, no backoff: the failure is client-shaped, e.g. TLS/decode).
  The fetcher still owns the fatal/transient KIND decision; this module only
  advises STRATEGY for non-fatal errors. Generic exceptions -> retry.
- normalize_url / fingerprint (Scrapy RFPDupeFilter spirit): canonical form
  (lowercase, default-port strip, utm_* drop, sorted query, fragment policy)
  + sha1(method|url|body|headers) hex. utm-strip alone kills ~5-15% dupes.
- Stats: status-code / retry-reason histograms, blocked count, per-host
  requests/bytes/errors. Module singleton + reset() for tests. (Phase-4 will
  persist snapshots; the crawler counts dict is left byte-identical.)
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TARGET_CONCURRENCY = 1.0
MAX_DELAY_S = 60.0
MAX_HOSTS = 2000
RETRY_AFTER_CAP_S = 300.0

_FATAL_STATUSES = frozenset({400, 401, 402, 403, 404, 405, 410, 451})
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 522, 524})
# Client-shaped failures where an alternate renderer plausibly succeeds.
_ESCALATE_MARKERS = ("ssl", "tls", "decompression", "charset", "406", "415",
                     "421", "505", "certificate")


@dataclass
class Slot:
    delay: float = 0.0
    rtt_ema: float | None = None


_slots: dict[str, Slot] = {}


def _slot(host: str) -> Slot:
    host = (host or "").lower()
    slot = _slots.get(host)
    if slot is None:
        if len(_slots) >= MAX_HOSTS:
            _slots.pop(next(iter(_slots)))
        slot = _slots[host] = Slot()
    return slot


def delay_for(host: str) -> float:
    """Current adaptive delay for host (0.0 when unseen). Extra floor only."""
    slot = _slots.get((host or "").lower())
    return slot.delay if slot else 0.0


def record(host: str, latency_s: float, ok: bool, retry_after: float = 0.0) -> float:
    """Fold one observation into the host slot. Returns the new delay.

    ok=True (HTTP 200): mean toward latency/target, clamped [0, 60].
    ok=False: freeze decreases (trouble must never speed us up); increases
    still apply. Retry-After always applies as a penalty floor.
    """
    slot = _slot(host)
    latency = max(0.0, float(latency_s or 0.0))
    target = latency / TARGET_CONCURRENCY
    new = (slot.delay + target) / 2.0
    new = max(target, new)
    new = min(max(0.0, new), MAX_DELAY_S)
    if ok or new > slot.delay:
        slot.delay = new
    if retry_after > 0:
        slot.delay = min(max(slot.delay, min(retry_after, MAX_DELAY_S)), MAX_DELAY_S)
    slot.rtt_ema = latency if slot.rtt_ema is None else 0.5 * slot.rtt_ema + 0.5 * latency
    return slot.delay


def reset_slots() -> None:
    _slots.clear()


def parse_retry_after(value: object) -> float:
    """Retry-After header value -> seconds (0.0 when absent/unparseable).

    Accepts delta-seconds ("120", "120.5") and HTTP-dates. Capped at 300s.
    """
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return min(max(0.0, float(text)), RETRY_AFTER_CAP_S)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(max(0.0, delta), RETRY_AFTER_CAP_S)
    except Exception:
        return 0.0


_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}


def normalize_url(url: str, keep_fragments: bool = False) -> str:
    """Canonical URL form for dedup keys. Unparseable input returns stripped input."""
    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        if not p.scheme or not p.hostname:
            return u
        scheme = p.scheme.lower()
        host = p.hostname.lower()
        port = p.port
        netloc = host
        if port and port != _DEFAULT_PORTS.get(scheme):
            netloc += f":{port}"
        if p.username:
            user = p.username + (f":{p.password}" if p.password else "")
            netloc = f"{user}@{netloc}"
        qsl = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
               if not k.lower().startswith("utm_")]
        qsl.sort()
        path = p.path or "/"
        frag = p.fragment if keep_fragments else ""
        return urlunparse((scheme, netloc, path, "", urlencode(qsl, doseq=True), frag))
    except Exception:
        return u


def fingerprint(method: str, url: str, body: bytes = b"",
                headers: dict | None = None) -> str:
    """Stable request identity: sha1 hex of canonical method|url|body|headers.

    Headers are opt-in (subset the caller passes, lowercased + sorted) —
    default ignores them, matching dupefilter convention.
    """
    subset = {}
    for k, v in (headers or {}).items():
        kl = str(k).lower()
        if kl in ("accept", "accept-language", "content-type", "authorization"):
            subset[kl] = str(v)
    payload = {"method": (method or "GET").upper(),
               "url": normalize_url(url),
               "body": bytes(body or b"").hex(),
               "headers": subset}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def strategy(exc: BaseException) -> str:
    """Advisory fetch strategy for a NON-FATAL error: 'retry' or 'escalate'.

    Fatal errors never reach here (the crawler branches on E_PROVIDER_FATAL
    first). 'escalate' means: skip the backoff and try the alternate method
    immediately — the failure is shaped like something a renderer handles
    better (TLS/decode/406-class). Everything else backs off and retries.
    """
    msg = (getattr(exc, "message", None) or str(exc) or "").lower()
    if any(m in msg for m in _ESCALATE_MARKERS):
        return "escalate"
    return "retry"


def is_fatal_status(status: int) -> bool:
    return status in _FATAL_STATUSES


def is_retry_status(status: int) -> bool:
    return status in _RETRY_STATUSES


def looks_js_shell(html: str, text_len: int, threshold: int) -> bool:
    """Thin page that rendering might rescue: little text BUT script present.

    A 200-with-113-chars page and no scripts (example.com) is complete, not
    broken — escalating it to a browser would burn ~10s for nothing. A thin
    page WITH scripts (SPA shell) is exactly what the renderer rung is for.
    """
    if text_len >= threshold or not html:
        return False
    return bool(re.search(r"<script[\s>]", html, re.IGNORECASE))


def has_hollow_code(html: str) -> bool:
    """Code blocks present but empty with scripts on the page (JS-injected code).

    Tutorial/doc sites often ship empty <pre>/<code> filled at runtime
    (syntax highlighters). The prose looks rich so thinness never fires, yet
    every code sample is missing. A browser render recovers them.
    """
    if not html or not re.search(r"<script[\s>]", html, re.IGNORECASE):
        return False
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        return any(not (el.get_text() or "").strip()
                   for el in soup.find_all(["pre", "code"]))
    except Exception:
        return False


def low_density_shell(html: str, clean_len: int, html_len: int) -> bool:
    """Big HTML, tiny text: the words live in client-side rendering.

    A 65KB shell yielding 1KB of text (1.6% density) is a JS app, not an
    article — no thinness threshold catches it because 1KB > 500 chars, yet
    98% of the content (article body, author, dates) never reaches the LLM.
    Thresholds: shell > 20KB, density < 5%, scripts present. Client-rendered
    shells are small AND scriptless without JS (then it's just a small page).
    """
    if html_len <= 20000 or clean_len <= 0 or not html:
        return False
    if clean_len / html_len >= 0.05:
        return False
    return bool(re.search(r"<script[\s>]", html, re.IGNORECASE))


class Stats:
    """Fetch observability: histograms + per-host ledger. In-memory; snapshot()."""

    def __init__(self) -> None:
        self.status_codes: dict[int, int] = {}
        self.retry_reasons: dict[str, int] = {}
        self.blocked: int = 0
        self.hosts: dict[str, dict] = {}
        self.started_at: str = datetime.now(timezone.utc).isoformat()

    def note_status(self, status: int) -> None:
        key = status if status > 0 else -1  # -1: no response (timeout/DNS)
        self.status_codes[key] = self.status_codes.get(key, 0) + 1

    def note_retry(self, reason: str) -> None:
        reason = (reason or "unknown")[:60]
        self.retry_reasons[reason] = self.retry_reasons.get(reason, 0) + 1

    def note_blocked(self) -> None:
        self.blocked += 1

    def note_host(self, host: str, nbytes: int = 0, ok: bool = True) -> None:
        host = (host or "").lower()
        entry = self.hosts.setdefault(host, {"requests": 0, "bytes": 0, "errors": 0})
        entry["requests"] += 1
        entry["bytes"] += max(0, int(nbytes or 0))
        if not ok:
            entry["errors"] += 1

    def snapshot(self) -> dict:
        return {"started_at": self.started_at,
                "status_codes": dict(self.status_codes),
                "retry_reasons": dict(self.retry_reasons),
                "blocked": self.blocked,
                "hosts": {h: dict(v) for h, v in self.hosts.items()},
                "delays": {h: round(s.delay, 3) for h, s in _slots.items() if s.delay > 0}}


stats = Stats()


def reset_stats() -> None:
    global stats
    stats = Stats()


def observe(host: str, status: int, latency_s: float, nbytes: int = 0,
            retry_after: float = 0.0) -> None:
    """Single observation entry point for fetch paths: slot + histograms."""
    ok = status == 200
    record(host, latency_s, ok, retry_after if status == 429 else 0.0)
    stats.note_status(status)
    stats.note_host(host, nbytes, ok)
    if status in (401, 403, 429):
        stats.note_blocked()
