# FastEmbed — local ONNX embeddings (no GPU)

Role: L3 embedder feeding Qdrant. Pinned model_name required.

Verified 2026-09-24 (via Qdrant agent):
- Repo https://github.com/qdrant/fastembed (v0.8.1) — Docs https://qdrant.tech/documentation/fastembed/fastembed-quickstart/
- Quote: "We don't require a GPU and don't download GBs of PyTorch dependencies, and instead use the ONNX Runtime."
```bash
pip install fastembed
# pip install fastembed-gpu  # CUDA only
```
Default docs model `BAAI/bge-small-en-v1.5` (384-d). Pin model + version, store point IDs in Postgres.
Sources: fastembed releases + qdrant.tech articles 2026-07-30, fetched 2026-09-24.
