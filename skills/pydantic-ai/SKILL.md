# PydanticAI — EVALUATE (no switch; Instructor stays)

Verified 2026-09-24: https://github.com/pydantic/pydantic-ai + https://pydantic.dev/docs/ai/ (agents: instructions+tools+output_type+deps)
```python
from pydantic import BaseModel
from pydantic_ai import Agent
class City(BaseModel):
    name: str; country: str
agent = Agent('openai:gpt-5-mini', output_type=list[City])
result = agent.run_sync('List 3 largest cities in Japan')
```
Instructor (thin extract, response_model+retries, ~3M/mo) vs PydanticAI (agent loop/DI/deps/stream-events/durable/evals). Own docs: extraction→Instructor, agents→PydanticAI. Pilot PydanticAI only if multi-tool runs/MCP/evals needed.
