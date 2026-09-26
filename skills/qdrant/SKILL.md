# Qdrant — L3 similarity index ONLY (Postgres = truth)

Role: ambiguous-case candidate matches only. Store qdrant_point_id in Postgres; never delete truth on miss.

Verified 2026-09-24:
- Repos https://github.com/qdrant/qdrant (v1.19.1, 04 Sep) + https://github.com/qdrant/fastembed (v0.8.1)
- Docs https://qdrant.tech/documentation/fastembed/fastembed-semantic-search/ + quickstart + cloud-quickstart
```bash
pip install "qdrant-client[fastembed]>=1.14.2"
docker run -p 6333:6333 -v "$(pwd)/qdrant_storage:/qdrant/storage:z" qdrant/qdrant
```
```python
from qdrant_client import QdrantClient, models
client = QdrantClient(":memory:")
model_name = "BAAI/bge-small-en"
client.create_collection("test_collection", vectors_config=models.VectorParams(size=client.get_embedding_size(model_name), distance=models.Distance.COSINE))
```
Free-tier: quoted only — "Create a Free Cluster..." — read Console/Calculator at deploy, do NOT hardcode GB/RPS.
Limits: IDs u64/UUID, reject size>65536/empty vectors, upsert idempotent, queue ~200, prod ≥3 nodes.
Sources: releases + docs above, fetched 2026-09-24.
