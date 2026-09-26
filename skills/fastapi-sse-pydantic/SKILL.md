# FastAPI + SSE + Pydantic — runner API

Role: 11 endpoints per 15-API-CONTRACT + GET /stream SSE. Pydantic v2 schemas = contracts/.

Verified 2026-09-24:
- https://fastapi.tiangolo.com/tutorial/server-sent-events/ + /reference/sse/ (native SSE ≥0.135.0) — https://docs.pydantic.dev/latest/ (v2.13.4) — https://github.com/sysid/sse-starlette
```bash
pip install fastapi uvicorn sse-starlette pydantic
```
```python
from fastapi import FastAPI
from sse_starlette import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
class RunEvent(BaseModel):
    job_id: str; status: str; progress: float = 0.0
@app.get("/stream")
async def stream(): return EventSourceResponse(gen(), ping=15)
```
Limits: server→client only, text/event-stream, per-conn buffer, proxies need X-Accel-Buffering:no + no-store, ping 15s, check is_disconnected, data XOR raw_data.
Note: agent-proposed /jobs + /health + /logs naming is NOT spec — use docs/15 endpoints.
