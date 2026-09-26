# Browser Use — STUDY ONLY (NOT MVP)

Study session lifecycle/history/final_result + local vs cloud browser only.

Verified 2026-09-24: https://github.com/browser-use/browser-use (~116k★, MIT) + https://docs.browser-use.com/open-source/quickstart
```bash
uv pip install browser-use
uvx browser-use install
```
```python
from browser_use import Agent, ChatBrowserUse
llm = ChatBrowserUse()
agent = Agent(task="Find the number 1 post on Show HN", llm=llm)
await agent.run()
```
Limits: OSS MIT, LLM+cloud pay separately ($15 one-time credit seen); no CAPTCHA guarantee; is_successful is agent-reported; stop cloud browsers explicitly.
