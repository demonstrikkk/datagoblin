# DuckDB — later analytics/export (Supabase = truth)

Role: snapshot → in-memory DuckDB → aggregate → CSV/Parquet. Parquet optional export.

Verified 2026-09-24: https://duckdb.org/docs/current/guides/python/install + clients/python/overview + data csv/parquet + limits + https://github.com/duckdb/duckdb (v1.5.5 seen 2026-07-22)
```bash
pip install duckdb  # Python 3.9+
```
```python
import duckdb
duckdb.sql("SELECT * FROM 'example.csv'")
duckdb.sql("SELECT 42").write_parquet("out.parquet")
```
Limits: 80% RAM buffer, string/BLOB 4GB, array 100k, expr depth 1000, per-task connection (no global across threads). Parquet Snappy default, zstd + ROW_GROUP_SIZE for exports.
