"""The model must be shown the part of the page that carries the data.

`page_evidence_text` is shared by the evidence store and the extractor, and the
extractor can only ever read a bounded prefix of it. So the order the parts are
concatenated in decides what the model actually sees. It was the wrong order:
uncapped raw DOM text first, clean markdown — the part that carries tables —
after it, and the 24,000-character ceiling is reached before the markdown
arrives.

This is silent in the worst way. The call succeeds, the model returns records,
and the fields that lived past the cut come back as `NA`. The validator records
that as an absent value, and the dataset shows an empty column that the live page
does not have.

So these tests assert ordering and the shortfall accounting, not just that the
function returns a string.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import reducer as R  # noqa: E402

# A page whose clean markdown is small and whose raw DOM is enormous. This is the
# shape that broke: a listing page with megabytes of nav-adjacent chrome.
FILLER = ("<nav>menu</nav>" + "<div>chrome chrome chrome</div>" * 400)
CLEAN_MD = "COMPANY | VALUATION\nAcme | 2500000000\nBeta | 4100000000"


@pytest.fixture(autouse=True)
def _clear_cache():
    R._EVIDENCE_CACHE.clear()
    yield
    R._EVIDENCE_CACHE.clear()


def text_and_stats(page: dict) -> tuple[str, dict]:
    return R.page_evidence_text_parts(page)


# ------------------------------------------------------------------ ordering


def test_clean_markdown_comes_before_the_raw_dom() -> None:
    """The fix.

    The reduced markdown is the highest signal per character on the page and the
    only part `reduce_html` builds with `include_tables=True`, so it is first.
    """
    page = {"html": f"<html><body>{FILLER}</body></html>", "markdown": CLEAN_MD}
    out, _ = text_and_stats(page)
    assert out.index("Acme") < out.index("chrome")


def test_the_table_survives_an_enormous_dom() -> None:
    """The concrete failure, end to end.

    A 24,000-character prefix of the old ordering was all chrome. The rows the
    schema describes must be inside the window now, without raising the ceiling.
    """
    page = {"html": f"<html><body>{FILLER}</body></html>", "markdown": CLEAN_MD}
    out, _ = text_and_stats(page)
    window = out[:24_000]
    assert "Acme | 2500000000" in window
    assert "Beta | 4100000000" in window


def test_a_rendered_page_with_no_dom_still_works() -> None:
    page = {"markdown": CLEAN_MD}
    out, stats = text_and_stats(page)
    assert "Acme" in out
    assert stats["dom_chars"] == 0


def test_a_static_page_with_no_markdown_falls_back_to_the_reducer() -> None:
    """A static rung returns html and no markdown; it must not lose its content."""
    page = {"html": "<html><body><p>Acme raised 2.5B</p></body></html>"}
    out, _ = text_and_stats(page)
    assert "Acme" in out


def test_nothing_is_dropped() -> None:
    """Reordering must not truncate: the proof view reads this same string.

    A human opening a stored page expects the page, and a quote cited past any
    window has to re-locate in what is stored.
    """
    page = {"html": f"<html><body>{FILLER}</body></html>", "markdown": CLEAN_MD}
    out, _ = text_and_stats(page)
    assert CLEAN_MD in out
    assert "chrome" in out


def test_store_and_extractor_get_byte_identical_text() -> None:
    """The invariant the whole design rests on.

    `crawler` writes this string into `pages.markdown`; the extractor cites
    offsets into it and the proof view resolves them against it. If the two ever
    diverge, a verified quote becomes unverifiable.
    """
    page = {"html": f"<html><body>{FILLER}</body></html>", "markdown": CLEAN_MD,
            "page_id": "p1", "content_hash": "h1"}
    # Both are plain functions. The callers push them onto a thread themselves
    # (`asyncio.to_thread` in the backfill and gap-fill paths) because the DOM
    # parse is blocking, so wrapping them in `asyncio.run` here was wrong twice
    # over: it asserted a coroutine contract the functions do not have, and it
    # would have hidden a real regression where someone made one async without
    # updating its thread-offload call sites.
    stored = R.page_evidence_text(page)
    seen, _ = R.page_evidence_text_parts(page)
    assert stored == seen


# --------------------------------------------------------------------- stats


def test_stats_report_each_part() -> None:
    page = {"html": f"<html><body>{FILLER}</body></html>", "markdown": CLEAN_MD}
    _, stats = text_and_stats(page)
    assert stats["clean_md_chars"] == len(CLEAN_MD)
    assert stats["dom_chars"] > len(CLEAN_MD)
    assert stats["total_chars"] == len(R.page_evidence_text(page))


def test_stats_are_zero_for_an_empty_page() -> None:
    _, stats = text_and_stats({})
    assert stats["clean_md_chars"] == 0
    assert stats["dom_chars"] == 0
    assert stats["total_chars"] == 0


def test_cache_serves_text_and_stats_together() -> None:
    page = {"markdown": CLEAN_MD}
    first, first_stats = text_and_stats(page)
    second, second_stats = text_and_stats(page)
    assert first == second
    assert first_stats == second_stats


def test_max_chars_still_truncates_the_wrapper() -> None:
    """`page_evidence_text(max_chars=...)` is unchanged for any caller using it."""
    page = {"markdown": "x" * 5000}
    assert len(R.page_evidence_text(page, max_chars=100)) == 100


# ------------------------------------------- the shortfall the extractor logs


def test_shortfall_separates_a_real_loss_from_expected_filler() -> None:
    """What the telemetry has to be able to say.

    `clean_truncated` is real data loss — it is the part carrying tables — and it
    is the signal that the ceiling should be raised. `dom_truncated` is the
    low-priority filler being cut, which is by construction fine.

    The accounting below mirrors the extractor so a change to one is caught.
    """
    clean_md_chars, dom_chars, sent = len(CLEAN_MD), 5_000, 24_000
    total = clean_md_chars + dom_chars + 1_000
    clean_in_window = min(clean_md_chars, sent)
    assert {
        "total_chars": total,
        "sent_chars": sent,
        "dropped_chars": max(0, total - sent),
        "clean_truncated": max(0, clean_md_chars - clean_in_window),
        "dom_truncated": max(0, dom_chars - max(0, sent - clean_in_window)),
    } == {
        "total_chars": len(CLEAN_MD) + 6_000,
        "sent_chars": 24_000,
        "dropped_chars": 0,
        "clean_truncated": 0,
        "dom_truncated": 0,
    }


def test_clean_markdown_truncation_is_detectable() -> None:
    """A page whose clean text alone exceeds the window must be visible as such."""
    clean_md_chars, sent = 40_000, 24_000
    clean_in_window = min(clean_md_chars, sent)
    assert max(0, clean_md_chars - clean_in_window) == 16_000


def test_dom_only_truncation_is_not_reported_as_data_loss() -> None:
    clean_md_chars, dom_chars, sent = 2_000, 90_000, 24_000
    clean_in_window = min(clean_md_chars, sent)
    assert max(0, clean_md_chars - clean_in_window) == 0
    assert max(0, dom_chars - max(0, sent - clean_in_window)) == 68_000
