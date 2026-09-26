"""Provider interfaces — the swappable seam (docs/05). Implementations in providers/*."""
from typing import Protocol


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int = 5) -> list[dict]: ...


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> dict:  # {url,title,html|markdown,method,status}
        ...


class LLMProvider(Protocol):
    async def structured_generate(self, prompt: str, schema: dict) -> dict: ...


class DedupEngine(Protocol):
    async def deduplicate(self, records: list[dict], keys: list[str]) -> list[dict]: ...
