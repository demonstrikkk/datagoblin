# Supabase — app truth (Postgres + Auth + Data API)

Role: workflows/runs/datasets/records/sources/run_events. JSONB for payloads. RLS on.

Verified 2026-09-24 (supabase.com/docs + supabase-py):
```bash
pip install supabase  # Python >3.8
```
```python
from supabase import create_client
supabase = create_client(url, key)
supabase.table("planets").insert({"id":1,"name":"Pluto"}).execute()
supabase.table("planets").select("*").execute()
```
Auth: publishable `sb_publishable_*` → anon/authenticated; secret `sb_secret_*` server-only → service_role BYPASSRLS. "Deprecating anon/service_role keys by end of 2026." Enable RLS + least-privilege grants + per-op policies.
Free (quoted): "two free projects" / "read-only when DB exceeds 500 MB" (data size, not disk). Limits: 500MB DB, 1GB disk/egress/storage, 50k MAU, select() max 1000 rows, Nano 60+200 conns.
Sources: docs installing/insert/select/RLS/api-keys/billing/database-size + supabase-py README.
