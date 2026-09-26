# LiteLLM — later provider router (Day1 direct Gemini allowed)

Role: same completion() call + fallbacks=[groq] or Router; Proxy later for keys/budgets.

Verified 2026-09-24: https://docs.litellm.ai/docs/ + completion/reliable_completions + proxy/reliability + providers/gemini|groq + https://github.com/BerriAI/litellm (~59k★)
```bash
pip install litellm  # needs Python 3.10+ since v1.84.0
```
```python
from litellm import completion
resp = completion(model="gemini/gemini-3.8-flash", messages=messages, fallbacks=["groq/llama-3.3-70b-versatile"])
```
Limits: fallback after num_retries, in-order; SDK loop ~45s, cooldown ~60s; v1.85 mock_testing_fallbacks stripped; enable_pre_call_checks for context-window.
