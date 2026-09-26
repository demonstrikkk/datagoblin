# LanceDB — EVALUATE (Phase 2 alternative index vs Qdrant)

Verified 2026-09-24: https://github.com/lancedb/lancedb (11.5k★, Apache-2.0; python v0.27.0 Jan 2026) + https://docs.lancedb.com/quickstart
```bash
pip install lancedb
```
```python
import lancedb
db = lancedb.connect("ex_lancedb")
table = db.create_table("characters", data=[{"id":"2","vector":[0.2,0.9,0.4,0.9]}], mode="overwrite")
table.search([0.2,0.8,0.4,0.9]).limit(2).to_polars()
```
Role: embedded vector+FTS+SQL candidate vs Qdrant (data-system fit). Brute-force <100k, IVF/HNSW via create_index. MVP stays RapidFuzz-only.
