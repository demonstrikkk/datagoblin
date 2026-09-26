# Presidio — LATER PII redact (not wired now)

Role: future analyzer→anonymizer before logs/store. No hackathon integration unless demo needs it.

Verified 2026-09-24: moved microsoft/presidio → https://github.com/data-privacy-stack/presidio (~11k★, MIT); docs installation + getting_started_text. Python 3.10–3.13.
```
pip install presidio_analyzer
pip install presidio_anonymizer
python -m spacy download en_core_web_lg
```
```python
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
results = AnalyzerEngine().analyze(text=text, entities=["PHONE_NUMBER"], language='en')
anonymized_text = AnonymizerEngine().anonymize(text=text, analyzer_results=results)
```
Caveat (quoted): "no guarantee that Presidio will find all sensitive information." Docker ghcr.io/data-privacy-stack/* (legacy mcr.* unmaintained).
