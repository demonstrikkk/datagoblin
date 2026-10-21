"""Where editions come from, and why a broken one cannot take the engine down.

Discovery is deliberately boring: read a JSON manifest from `editions/<name>/`
or accept a profile registered by an entry point. No edition module is imported
by core, so a syntax error, a missing dependency or an infinite import loop in a
vertical is a *reported* edition error, not a dead API process.

`get()` is the single read path. It is cached because plan compilation runs per
request; `reset()` exists so tests and long-running development are not stuck
with a manifest they just edited.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from app.core.config import settings
from app.domain.profile import (EDITION_API_VERSION, DomainProfile,
                                generic_profile)

_ENTRY_POINT_GROUP = "datagoblin.editions"

_CACHE: dict[str, "Edition"] | None = None


@dataclass(frozen=True)
class Edition:
    """A profile and the provenance needed to explain where it came from."""

    name: str
    profile: DomainProfile | None
    source: str                     # builtin | manifest | entry-point | registered
    path: str = ""
    error: str = ""                 # fatal: the edition is not usable
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return self.profile is not None and not self.error


def repo_root() -> Path:
    """The repository root, for resolving the default `editions/` directory."""
    return Path(__file__).resolve().parents[3]


def editions_path() -> Path:
    """Configured editions directory, or `<repo>/editions`."""
    configured = (getattr(settings, "EDITIONS_PATH", "") or "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (repo_root() / path)
    return repo_root() / "editions"


def _reject(profile: DomainProfile) -> str:
    """Why this profile cannot be used by this core, or "" when it can."""
    if int(profile.api_version) != EDITION_API_VERSION:
        return (f"edition API {profile.api_version} is not supported; this "
                f"core speaks API {EDITION_API_VERSION}")
    return ""


def _load_manifest(directory: Path) -> Edition:
    manifest = directory / "edition.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        profile = DomainProfile.model_validate(data)
    except FileNotFoundError:
        return Edition(directory.name, None, "manifest", str(manifest),
                       error="edition.json is missing")
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        return Edition(directory.name, None, "manifest", str(manifest),
                       error=f"invalid edition.json: {str(exc)[:400]}")

    why = _reject(profile)
    if why:
        return Edition(profile.name, None, "manifest", str(manifest), error=why)

    warnings: list[str] = []
    raw = (profile.selectors_dir or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = (directory / path).resolve()
        if path.is_dir():
            profile = profile.model_copy(update={"selectors_dir": str(path)})
        else:
            warnings.append(f"selectors_dir does not exist: {path}")

    return Edition(profile.name, profile, "manifest", str(manifest),
                   warnings=tuple(warnings))


def _from_entry_points() -> Iterable[Edition]:
    """Profiles installed as packages, if the environment exposes any.

    Entry points are read but never trusted: any exception is turned into an
    `Edition` with an error, because a broken third-party distribution must not
    make `git clone && pytest` impossible.
    """
    try:
        from importlib.metadata import entry_points
    except Exception:  # pragma: no cover - importlib.metadata is stdlib on 3.11+
        return ()
    try:
        points = entry_points()
        selected = (points.select(group=_ENTRY_POINT_GROUP)
                    if hasattr(points, "select")
                    else points.get(_ENTRY_POINT_GROUP, []))
    except Exception:
        return ()
    out: list[Edition] = []
    for point in selected:
        name = getattr(point, "name", "") or "unknown"
        try:
            loaded = point.load()
            profile = None
            if isinstance(loaded, DomainProfile):
                profile = loaded
            elif isinstance(getattr(loaded, "profile", None), DomainProfile):
                profile = loaded.profile
            elif isinstance(getattr(loaded, "PROFILE", None), DomainProfile):
                profile = loaded.PROFILE
            else:
                out.append(Edition(name, None, "entry-point",
                                   error="entry point did not expose a DomainProfile"))
                continue
            why = _reject(profile)
            out.append(Edition(profile.name, None if why else profile,
                               "entry-point", error=why))
        except Exception as exc:
            out.append(Edition(name, None, "entry-point",
                               error=f"could not load: {str(exc)[:300]}"))
    return out


def load(refresh: bool = False) -> dict[str, Edition]:
    """Every edition the core can see, keyed by name. Never raises."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE

    found: dict[str, Edition] = {"generic": Edition(
        "generic", generic_profile(), "builtin")}

    root = editions_path()
    try:
        directories = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        directories = []
    for directory in directories:
        edition = _load_manifest(directory)
        found[edition.name] = edition

    for edition in _from_entry_points():
        found[edition.name] = edition

    _CACHE = found
    return found


def get(name: str) -> Edition | None:
    """One edition by name, or None when it is not installed at all."""
    key = str(name or "").strip()
    if not key:
        return None
    return load().get(key)


def register(profile: DomainProfile, *, source: str = "registered") -> Edition:
    """Add or replace an edition in-process (tests, embedded deployments)."""
    why = _reject(profile)
    edition = Edition(profile.name, None if why else profile, source,
                      error=why)
    load().setdefault("generic", Edition("generic", generic_profile(), "builtin"))
    assert _CACHE is not None
    _CACHE[edition.name] = edition
    return edition


def list_editions() -> list[dict]:
    """Metadata for an API or a UI. Never includes a traceback."""
    return [{"name": e.name, "version": (e.profile.version if e.profile else ""),
             "api_version": (e.profile.api_version if e.profile else 0),
             "description": (e.profile.description if e.profile else ""),
             "source": e.source, "status": "ok" if e.usable else "error",
             "error": e.error, "warnings": list(e.warnings),
             "examples": (list(e.profile.examples) if e.profile else [])}
            for e in sorted(load().values(), key=lambda e: e.name)]


def reset() -> None:
    """Forget discovery. Tests use it; a process restart is the production path."""
    global _CACHE
    _CACHE = None
