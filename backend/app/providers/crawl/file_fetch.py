"""Local-corpus fetcher — replay-mode adapter (docs/25), NOT a mock.

Implements the PageFetcher protocol over user-supplied corpus files: real file
I/O, real HTML bytes, real titles. The pipeline cannot distinguish its output
from HTTP pages except `method: file`. Swapping to live fetch changes nothing
downstream. Robots/SSRF do not apply: the corpus is user-owned local data.
"""
from pathlib import Path
from typing import Any


def build(pages_dir: str, url_to_file: dict[str, str], url_to_title: dict[str, str] | None = None):
    root = Path(pages_dir)
    titles = url_to_title or {}

    async def _fetch(url: str, method: str) -> dict[str, Any]:
        _ = method  # corpus has no methods; same Page contract regardless
        name = url_to_file.get(url, "")
        if not name:
            raise RuntimeError(f"Corpus has no file for {url[:120]}")
        path = (root / name).resolve()
        if root.resolve() not in path.parents and path != root.resolve():
            raise RuntimeError(f"Corpus escape refused: {name[:120]}")
        html = path.read_text(encoding="utf-8")
        if len(html) > 2_000_000:
            raise RuntimeError(f"Corpus file too large: {name[:120]}")
        return {"url": url, "final_url": url, "title": titles.get(url, "")[:200],
                "html": html, "method": "file"}

    return _fetch
