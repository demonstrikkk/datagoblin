"""No module may read a global name it never binds, in any scope.

Two 500s shipped past a green suite because the existing hygiene test only
matched names shaped like `foo_svc`:

    main.py:936  re.match(...)          -> NameError on every non-empty column name
    main.py:1067 dependency(...)        -> NameError on every /query error path

Both are plain global reads with no binding anywhere in the file. Nothing about
them is service-shaped, so `SVC = re.compile(r"^[a-z][a-z0-9_]*_svc$")` could
never see them, and no test exercised either route.

So this check is not "import the module and hope". It is a static scope
resolver over the AST: for every `Name` load in `app/`, is that name bound in
the enclosing scopes, or is it a builtin? That is the general form of the rule,
and because it needs no imports to run, an ImportError elsewhere cannot hide it.
"""
from __future__ import annotations

import ast
import builtins
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

BUILTINS = frozenset(dir(builtins)) | {
    # Module dunders. Not all are real builtins, but every module may read them.
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__debug__",
}

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in APP.rglob("*.py"))


# --------------------------------------------------------------------------
# binding collection


def _target_names(node: ast.AST, out: set[str]) -> None:
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _target_names(elt, out)
    elif isinstance(node, ast.Starred):
        _target_names(node.value, out)


def _defines(node: ast.AST, out: set[str]) -> None:
    """Bind the name a `def`/`class` statement introduces into `out`.

    `_bind` skips nested scopes, which is right for their *bodies* but wrong for
    the module-level statement itself: `class AppError:` binds `AppError` in the
    enclosing scope even though its body belongs to a new one.
    """
    if isinstance(node, _SCOPES):
        out.add(node.name)
        return
    if isinstance(node, (ast.If, ast.Try, ast.While, ast.For, ast.AsyncFor,
                         ast.With, ast.AsyncWith)):
        for field in ("body", "orelse", "finalbody", "handlers"):
            for stmt in getattr(node, field, []) or []:
                _defines(stmt, out)
    elif isinstance(node, ast.ExceptHandler):
        for stmt in node.body:
            _defines(stmt, out)
    elif isinstance(node, ast.Match):
        for case in node.cases:
            for stmt in case.body:
                _defines(stmt, out)


def _bind(node: ast.AST, out: set[str]) -> None:
    """Every name this scope binds.

    Deliberately does NOT descend into nested function/class bodies: a name
    assigned inside `def inner()` is not bound in the enclosing function, and
    treating it as such is precisely the bug class `politeness_svc` was.
    Comprehension targets ARE collected — they live in their own scope, so
    including them is the lenient choice and avoids false positives.
    """
    _defines(node, out)
    if isinstance(node, _SCOPES):
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            out.add((alias.asname or alias.name).split(".")[0])
        return
    if isinstance(node, ast.Assign):
        for target in node.targets:
            _target_names(target, out)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        _target_names(node.target, out)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        _target_names(node.target, out)
    elif isinstance(node, ast.withitem):
        if node.optional_vars is not None:
            _target_names(node.optional_vars, out)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            out.add(node.name)
    elif isinstance(node, ast.NamedExpr):
        _target_names(node.target, out)
    for child in ast.iter_child_nodes(node):
        _bind(child, out)


def _declared_global(fn: ast.AST) -> set[str]:
    """Names this one function declares `global`/`nonlocal`.

    Scoped to the function's own body. `ast.walk` would be wrong here: a nested
    `def inner(): nonlocal counter` makes `walk` report `counter` as non-local to
    the *outer* function too, and the outer binding — which is the one that
    actually exists — gets deleted. Only direct statements of this scope count,
    so this recurses through control flow but never into another scope.
    """
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    out: set[str] = set()

    def walk(node: ast.AST) -> None:
        if node is not fn and isinstance(node, _SCOPES):
            return  # a nested scope's declarations are its own business
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in getattr(fn, "body", []):
        walk(stmt)
    return out


def _param_names(fn: ast.AST) -> set[str]:
    if isinstance(fn, ast.ClassDef) or not hasattr(fn, "args"):
        return set()
    a = fn.args
    names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _has_star_import(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    )


def _uses_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def _annotation_ids(tree: ast.Module) -> frozenset[int]:
    """id() of every AST node living inside a type annotation.

    Under `from __future__ import annotations` an annotation is never evaluated,
    so a TYPE_CHECKING-only import inside it is legitimate and must not be
    reported. Those files get their annotations skipped entirely.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                if arg.annotation:
                    targets.append(arg.annotation)
            for extra in (a.vararg, a.kwarg):
                if extra is not None and extra.annotation:
                    targets.append(extra.annotation)
            returns = getattr(node, "returns", None)
            if returns is not None:
                targets.append(returns)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            targets.append(node.annotation)
        for ann in targets:
            for sub in ast.walk(ann):
                out.add(id(sub))
    return frozenset(out)


# --------------------------------------------------------------------------
# scope resolution


def _resolve(tree: ast.Module, module_visible: frozenset[str],
             skip_ids: frozenset[int]) -> list[tuple[str, int]]:
    """Every (name, lineno) read in `tree` that no enclosing scope binds."""
    found: list[tuple[str, int]] = []

    def scan(node: ast.AST, vis: frozenset[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if id(child) in skip_ids:
                continue
            if isinstance(child, _COMPREHENSIONS):
                # A comprehension is its own scope, but binds only its iteration
                # variables. Widening by those is the lenient, safe direction.
                inner_vis = set(vis)
                for gen in child.generators:
                    _target_names(gen.target, inner_vis)
                scan(child, frozenset(inner_vis))
                continue
            if isinstance(child, (_SCOPES, ast.Lambda)):
                enter(child, vis)
                continue
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, ast.Load) and child.id not in vis:
                    found.append((child.id, child.lineno))
                continue
            scan(child, vis)

    def enter(fn: ast.AST, outer: frozenset[str]) -> None:
        # A lambda is a scope with no statements — parameters plus one expression.
        body: list[ast.AST] = [fn.body] if isinstance(fn, ast.Lambda) else list(fn.body)
        local: set[str] = _param_names(fn)
        for stmt in body:
            _bind(stmt, local)
        if isinstance(fn, ast.ClassDef):
            local = set()  # a class body binds no names for its own scope
        # `global x` means x lives at module scope and `nonlocal x` means it lives
        # in an enclosing function. Both stop being local bindings here; both stay
        # visible through `outer`, which already contains the module and the
        # enclosing function's names. So dropping them is correct, not a loss.
        local -= _declared_global(fn)

        if isinstance(fn, ast.ClassDef):
            inner = frozenset(outer | local)
            for stmt in fn.body:
                if isinstance(stmt, _SCOPES):
                    # A method sees its own class body, then the enclosing scope.
                    enter(stmt, inner)
                else:
                    scan(stmt, inner)
            return

        inner_vis = frozenset(outer | local)
        for stmt in body:
            if isinstance(stmt, _SCOPES):
                enter(stmt, inner_vis)
            else:
                scan(stmt, inner_vis)

    for stmt in tree.body:
        if isinstance(stmt, _SCOPES):
            enter(stmt, module_visible)
        else:
            scan(stmt, module_visible)
    return found


def _unbound_in(path: pathlib.Path) -> list[tuple[str, int]]:
    """Unbound global reads in `path`, excluding skipped (star-import) modules."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))

    module_bound = set(BUILTINS)
    for stmt in tree.body:
        _bind(stmt, module_bound)
    module_bound -= _declared_global(tree)

    skip_ids = _annotation_ids(tree) if _uses_future_annotations(tree) else frozenset()
    return _resolve(tree, frozenset(module_bound), skip_ids)


def _report(path: pathlib.Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for name, lineno in sorted(set(_unbound_in(path)), key=lambda t: (t[1], t[0])):
        ctx = lines[lineno - 1].strip()[:88] if 0 < lineno <= len(lines) else ""
        out.append(f"{name} at line {lineno} -> {ctx}")
    return out


# --------------------------------------------------------------------------
# tests


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(APP.parent)))
def test_no_module_reads_a_global_it_never_binds(path: pathlib.Path) -> None:
    if _has_star_import(ast.parse(path.read_text(encoding="utf-8"))):
        pytest.skip("`from x import *` makes static resolution unsound")
    findings = _report(path)
    assert not findings, (
        f"{path.relative_to(APP.parent)} reads {len(findings)} name(s) bound in no "
        f"enclosing scope. Each is a NameError the instant that line runs:\n  "
        + "\n  ".join(findings)
        + "\nBind it at module scope, or inside the function that uses it."
    )


@pytest.mark.parametrize("name", ["re", "dependency"])
def test_the_shipped_nameerrors_cannot_return(name: str) -> None:
    """Named regressions, so the general rule above cannot be quietly weakened."""
    offenders = [str(p.relative_to(APP.parent)) for p in _modules()
                 if not _has_star_import(ast.parse(p.read_text(encoding="utf-8")))
                 and any(n == name for n, _ in _unbound_in(p))]
    assert not offenders, (
        f"`{name}` is read without a module-scope binding in {offenders}.")


def test_resolver_flags_a_planted_miss() -> None:
    """A guard that cannot fail is not a guard."""
    bad = ast.parse("""
def handler(req):
    if re.match("x", name):
        return dependency("boom")
    return json.dumps({})
""")
    found = {n for n, _ in _resolve(bad, BUILTINS | {"handler"}, frozenset())}
    assert {"re", "dependency"} <= found, f"resolver only saw {sorted(found)}"


def test_resolver_is_clean_on_correct_code() -> None:
    ok = ast.parse("""
import re
from app.core.errors import dependency

def handler(req, name):
    return dependency(re.match("x", name) and "boom")
""")
    assert not _resolve(ok, BUILTINS | {"handler", "re", "dependency"}, frozenset())


def test_resolver_respects_function_local_shadowing() -> None:
    """A function-local import must never be reported at module scope."""
    tree = ast.parse("""
import re

def one():
    from app.core.errors import dependency
    return dependency("a")

def two():
    return re.match("b", "c")
""")
    assert not _resolve(tree, BUILTINS | {"re"}, frozenset())


def test_resolver_allows_builtins_comprehensions_and_dunders() -> None:
    tree = ast.parse("""
def f(x):
    try:
        return len({k: v for k, v in x.items()}) + int(str(bool(x))) + sum([1, 2])
    except ValueError:
        import json
        return json.dumps({"name": __name__, "n": len(x)})
""")
    assert not _resolve(tree, BUILTINS | {"f"}, frozenset())


def test_resolver_allows_class_scope_and_methods() -> None:
    tree = ast.parse("""
DEFAULT = 3

class Cfg:
    limit = DEFAULT

    def check(self, value):
        return value <= self.limit

    class Inner:
        n = DEFAULT
""")
    assert not _resolve(tree, BUILTINS | {"DEFAULT"}, frozenset())


def test_resolver_handles_loop_except_with_and_star_targets() -> None:
    tree = ast.parse("""
def f(items):
    total = []
    for row in items:
        head, *tail = row
        total.append((head, tail))
    try:
        pass
    except ValueError as exc:
        total.append(exc)
    else:
        total.append("ok")
    finally:
        pass
    with open("x") as fh:
        total.append(fh)
    return total
""")
    assert not _resolve(tree, BUILTINS | {"f"}, frozenset())


def test_resolver_ignores_deferred_annotations() -> None:
    """Under `from __future__ import annotations`, TYPE_CHECKING names are fine."""
    tree = ast.parse("""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from decimal import Decimal

def f(x: Decimal) -> Decimal:
    return x
""")
    assert not _resolve(tree, BUILTINS | {"f", "TYPE_CHECKING"}, _annotation_ids(tree))