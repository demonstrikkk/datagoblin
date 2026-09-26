"""Document provider — Docling Phase-1.5 adapter (lazy import keeps MVP installs tiny)."""
def extract_document(path_or_bytes, filename: str = "") -> str:
    try:
        from docling.document_converter import DocumentConverter  # optional dep
        conv = DocumentConverter()
        res = conv.convert(path_or_bytes)
        return (res.document.export_to_markdown() or "")[:30_000]
    except ImportError:
        raise RuntimeError("docling not installed (Phase 1.5 optional). pip install docling")
