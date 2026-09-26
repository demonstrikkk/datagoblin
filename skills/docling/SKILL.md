# Docling — document provider (Phase 1.5, NOT MVP core)

Role: `document:docling` branch in source_router. PDF/DOCX/PPTX/XLSX/HTML/OCR → markdown → Extract.

Verified 2026-09-24: https://github.com/docling-project/docling (67.9k★, MIT, IBM/LF) + https://docling-project.github.io/docling/ (quickstart/supported_formats/installation)
```bash
pip install docling  # py>=3.10; CPU: --extra-index-url https://download.pytorch.org/whl/cpu
```
```python
from docling.document_converter import DocumentConverter
result = DocumentConverter().convert("https://arxiv.org/pdf/2408.09869")
print(result.document.export_to_markdown())
```
Formats: PDF/DOCX/XLSX/PPTX/HTML/CSV/EPUB/images/audio(asr)/XML; tables + OCR pluggable (EasyOCR/RapidOCR/Tesseract). Limits: heavy (Torch+model downloads, slow cold-start), needs Tesseract/LibreOffice/ffmpeg for some formats; models separate licences. Code: providers/crawl/docling.py (lazy import, raises clear error if uninstalled).
