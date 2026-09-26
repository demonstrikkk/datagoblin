# Instructor — Planner/Extractor → Pydantic

Role: Planner→Pydantic + Extractor→Pydantic with retries/validation. Never invents flow.

Verified 2026-09-24:
- Repo https://github.com/jxnl/instructor/ (redirects 567-labs/instructor, MIT) — Docs https://python.useinstructor.com/
- Latest CHANGELOG v1.17.1 (2026-09-09); PyPI lag showed 1.17.0 — patch date UNVERIFIED. Python >=3.9,<4.0.
```bash
pip install instructor
pip install "instructor[google-genai]"
```
```python
import instructor
from pydantic import BaseModel
client = instructor.from_provider("google/gemini-3.8-flash")
class User(BaseModel):
    name: str; age: int
resp = client.create(messages=[{"role":"user","content":"Extract Jason is 25"}], response_model=User)
```
Limits: Gemini no Union except Optional; OpenAI chat needs reasoning_effort none; GenAI MAX_TOKENS non-stream raises IncompleteOutputException; use max_retries=3, create_iterable/Partial for lists/stream.
Sources: repo+CHANGELOG, docs integrations openai/google.
