"""Every `*_svc` name used in a module must be bound in that module.

`main.py` called `politeness_svc.fingerprint()` inside `_reuse`, but the only
import binding that name was *function-local* to `intel_ask`. `_reuse` therefore
raised `NameError`, `crawler.fetch_all` caught it and fell through to a real
fetch, and stored-page reuse never engaged: every run silently re-downloaded
every URL it already held. The reuse unit tests passed the whole time, because
they inject their own `_reuse` closure and never exercise the one the HTTP path
uses.

The lesson is not "add the import" — it is that a test suite can cover a
component's contract and still miss that the wiring above it is broken. So the
check is static and general: for each module, every `foo_svc` attribute access
must have a binding visible at module scope. It needs no imports to run, so it
cannot be broken by an import error.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: The convention this project uses for injected service modules.
SVC = re.compile(r"^[a-z][a-z0-9_]*_svc$")


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in APP.rglob("*.py"))


def _module_bound_names(tree: ast.Module) -> set[str]:
    """Names bound at module scope, following imports and definitions.

    Function-local imports are deliberately excluded: they bind inside one
    function only, and using that name elsewhere is exactly the bug this catches.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _local_imports(tree: ast.Module) -> set[str]:
    """Names bound by an import that lives inside a function."""
    local: set[str] = set()
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for stmt in fn.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        local.add(a.asname or a.name)
    return local


def test_every_app_module_parses():
    for path in _modules():
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function_scope(tree: ast.Module) -> list[tuple[ast.AST, set[str]]]:
    """(node, names bound *inside* it) for every function and method.

    Scope matters. A service imported at the top of a function and used only in
    that function is not a bug — it is how you avoid a circular import. The bug
    is a name used in a scope that never bound it.
    """
    out = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        bound: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    bound.add(a.asname or a.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            bound.add(n.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
        out.append((fn, bound))
    return out


def _svc_uses(node: ast.AST) -> set[str]:
    return {n.value.id for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and SVC.match(n.value.id)}


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_module_uses_a_service_it_never_imported(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_bound = _module_bound_names(tree)
    scopes = _function_scope(tree)

    failures: list[str] = []
    for node, local in scopes:
        unbound = _svc_uses(node) - module_bound - local
        if unbound:
            where = getattr(node, "name", "?")
            failures.append(f"{where}() uses {sorted(unbound)}")

    # Module-level uses (outside any function) must be bound at module level.
    top_uses = _svc_uses(tree) - set().union(*[_svc_uses(n) for n, _ in scopes]) \
        if scopes else _svc_uses(tree)
    unbound_top = top_uses - module_bound
    if unbound_top:
        failures.append(f"module scope uses {sorted(unbound_top)}")

    assert not failures, (
        f"{path.name}: " + "; ".join(failures)
        + " — a function-local import is only visible in that function")



def test_the_reuse_closure_is_reachable_by_name():
    """The specific regression, pinned so it cannot come back silently.

    A green reuse suite is not evidence that reuse works: these tests inject their
    own closure, so the HTTP path's `_reuse` was never called by anything.
    """
    src = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bound = _module_bound_names(tree)
    assert "politeness_svc" in bound, (
        "main.py binds politeness_svc only inside a function, so _reuse raises "
        "NameError and every run re-fetches everything it already has")
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "_reuse")
    names = {n.value.id for n in ast.walk(fn)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert "politeness_svc" in names, "_reuse no longer fingerprints the request"


def test_main_defines_the_service_closures_the_runner_context_needs():
    """Every `ctx` key the runner reads must be wired in `POST /api/runs`.

    The runner reads `ctx["search"]` and `ctx["store"]` with `[]` — a KeyError, not
    a graceful skip — and everything else with `.get()`, so a missing optional
    hook silently disables a feature instead of failing.
    """
    src = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    handler = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_run"), None)
    assert handler is not None, "POST /api/runs handler `start_run` not found"

    # The ctx dict is assigned inside the handler, so find the literal rather than
    # the function itself.
    ctx = next((n for n in ast.walk(handler)
                if isinstance(n, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == "search"
                        for k in n.keys)), None)
    assert ctx is not None, "could not find the ctx dict built in start_run"

    keys = {k.value for k in ctx.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    for required in ("search", "fetch", "llm", "store", "persist_page",
                     "persist_source", "reuse", "cancelled", "llm_preflight"):
        assert required in keys, (
            f"ctx is missing {required!r} — the runner reads `search` and `store` "
            f"with [] and would raise KeyError, and the rest with .get() so a "
            f"missing hook silently disables the feature")

