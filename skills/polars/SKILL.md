# Polars — EVALUATE (Phase 2 transform; Polars=transform, DuckDB=query)

Verified 2026-09-24: https://github.com/pola-rs/polars (39.9k★, MIT, Rust engine) + https://docs.pola.rs/user-guide/installation/ + sql/intro/
```bash
pip install polars
```
```python
import polars as pl
df = pl.DataFrame({"a": [1,2,3]})
ctx = pl.SQLContext(t=df, eager=True); print(ctx.execute("SELECT * FROM t"))
```
Role: normalize/transform dataframes after Validate; DuckDB stays for SQL/export. Do NOT add to MVP (Pydantic+Python enough).
