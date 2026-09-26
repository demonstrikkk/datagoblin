# Verifier rules (deterministic reference for code, see 11-VALIDATION-SPEC)

string→str, number→float|int, boolean→bool, date→ISO-8601, url→valid URL, array→list.
required+missing→invalid; unparseable→invalid; no quote→unverified (null); quote∉source_text→unverified; conflicts→conflicting. No confidence %.
