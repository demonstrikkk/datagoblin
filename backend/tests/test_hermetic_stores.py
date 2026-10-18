"""No test may reach the developer's real store.

`TestClient(app)` runs the lifespan, and the lifespan builds the repository from
the process CWD. So a test that constructs one without isolating the store is
not a unit test -- it is a request against whatever `.datagoblin-local/` happens
to contain on the machine.

Two did. Both are in this file's history and both were invisible in the summary
output, because:

  * `test_api_key_gate.py` read the real datasets while asserting about auth, so
    a pass meant "auth works AND your data loaded", which is not a claim anyone
    can act on.
  * `test_mutating_routes.py` went further and POSTed to `/api/datasets/{id}/gaps/fill`
    against the first dataset it found, then `pytest.skip`-ed out of existence
    when that store was empty. So the coverage of a route that once answered 500
    for every dataset, always, depended on whether you had run a dataset lately.

That second one is the pattern worth forbidding. A test that skips when the
precondition is absent is not a weaker test, it is a test that reports success
without having checked anything.

Isolation is one of: `chdir` into tmp, or replacing the repository outright.
Both are accepted here; leaving the store alone is not.
"""
import pathlib
import re

TESTS = pathlib.Path(__file__).resolve().parent

#: Constructs a client bound to the real application, and so to the real lifespan.
_USES_APP_CLIENT = re.compile(r"TestClient\(\s*(?:main_mod\.)?app\s*\)")

#: Any of these means the test pointed the app somewhere disposable.
_ISOLATES = (
    "chdir(",                  # tmp_path store
    "build_repo",              # monkeypatched repo factory
    'setattr(main_mod, "REPO"',  # injected repository
    'setattr(main_mod, "REPO"',
)


def _offenders():
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        if not _USES_APP_CLIENT.search(src):
            continue
        if not any(token in src for token in _ISOLATES):
            out.append(path.name)
    return out


def test_every_app_client_test_isolates_the_store():
    assert not _offenders(), (
        "these tests build a TestClient against the real app without pointing "
        "it at a throwaway store, so they read (and may write) the developer's "
        "real .datagoblin-local/:\n  "
        + "\n  ".join(_offenders())
        + "\n\nAdd `monkeypatch.chdir(tmp_path)`, or monkeypatch build_repo / "
          "main_mod.REPO."
    )


def test_no_test_silently_skips_on_absent_data():
    """A skip on a missing precondition is success without a check.

    Narrow on purpose: it only fires where a test both skips and reaches for
    stored data, which is the combination that turns coverage into a coin toss.
    """
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue  # this file quotes the pattern to explain it
        src = path.read_text(encoding="utf-8")
        if "pytest.skip" not in src:
            continue
        if "no datasets stored" in src:
            offenders.append(path.name)
    assert not offenders, (
        "these tests skip when the local store is empty, so their coverage is "
        "a function of what you last ran:\n  " + "\n  ".join(offenders))


def test_the_guard_would_notice_the_defect_it_describes():
    """The guard itself has to be able to fail.

    A static guard that passes no matter what is the same failure it exists to
    prevent -- a check that cannot fail is not a check. This asserts the matcher
    recognises an unisolated client and rejects one.
    """
    unisolated = "with TestClient(main_mod.app) as client:\n    pass\n"
    isolated = ("monkeypatch.chdir(tmp_path)\n"
                "with TestClient(main_mod.app) as client:\n    pass\n")
    assert _USES_APP_CLIENT.search(unisolated)
    assert _USES_APP_CLIENT.search(isolated)
    assert not any(t in unisolated for t in _ISOLATES)
    assert any(t in isolated for t in _ISOLATES)