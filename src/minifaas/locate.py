"""Find the module that declares the functions, anywhere in a checkout.

The supervisor never imports user code, so this reads files as text: a module is a candidate
when it imports functions_framework or firebase_functions *and* has a decorator. That is
enough to pick the file; discover.py then imports it for real and reads the exact triggers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from minifaas.errors import ReleaseBuildError

_IMPORTS = re.compile(r"^\s*(?:import|from)\s+(?:functions_framework|firebase_functions)\b", re.MULTILINE)
_DECORATOR = re.compile(r"^\s*@", re.MULTILINE)

#: Never where a project's functions live, and often huge.
_SKIP_DIRS = {"venv", "node_modules", "__pycache__", "site-packages", "tests", "test", "examples", "docs"}
_MAX_FILES = 2000


@dataclass(frozen=True)
class Entry:
    source_dir: Path
    module: str

    def describe(self, checkout: Path) -> str:
        return str((self.source_dir / f"{self.module}.py").relative_to(checkout))


def _declares_functions(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return bool(_IMPORTS.search(text) and _DECORATOR.search(text))


def candidates(root: Path) -> list[Path]:
    """Python files under ``root`` that declare functions, shallowest first."""
    found: list[Path] = []
    scanned = 0
    for path in sorted(root.rglob("*.py"), key=lambda p: (len(p.relative_to(root).parts), str(p))):
        parts = path.relative_to(root).parts[:-1]
        if any(part.startswith(".") or part in _SKIP_DIRS for part in parts):
            continue
        scanned += 1
        if scanned > _MAX_FILES:
            break
        if _declares_functions(path):
            found.append(path)
    return found


def find_entry(checkout: Path, source_root: str, entry_module: str) -> Entry:
    """Where to import from: the configured folder and module if set, otherwise whatever declares functions.

    Several candidates are fine when one is a main.py (the Functions Framework default); otherwise the
    choice would be a guess, so the build stops and names them.
    """
    root = checkout / source_root if source_root else checkout
    if not root.is_dir():
        raise ReleaseBuildError(f"The source folder {source_root!r} does not exist in this commit.")

    if entry_module:
        if (root / f"{entry_module}.py").exists():
            return Entry(root, entry_module)
        raise ReleaseBuildError(f"There is no {entry_module}.py in {source_root or 'the repository root'}.")

    found = candidates(root) if not source_root else [p for p in root.glob("*.py") if _declares_functions(p)]
    mains = [p for p in found if p.name == "main.py"]
    if len(found) == 1 or mains:
        chosen = mains[0] if mains else found[0]
        return Entry(chosen.parent, chosen.stem)

    if found:
        names = ", ".join(str(p.relative_to(checkout)) for p in found[:8])
        raise ReleaseBuildError(
            f"More than one file declares functions ({names}). Pick one with the source folder and entry module "
            "on the Settings page."
        )
    raise ReleaseBuildError(
        "No functions found in this commit. miniFaaS runs Python functions decorated with "
        "@functions_framework.http (HTTP), @https_fn.on_request (HTTP) or @scheduler_fn.on_schedule (cron), "
        "and none of the repository's .py files import functions_framework or firebase_functions."
    )


def requirements_for(entry: Entry, checkout: Path) -> Path | None:
    """The nearest requirements.txt or pyproject.toml, from the entry module's folder up to the repo root."""
    folder = entry.source_dir
    while True:
        for name in ("requirements.txt", "pyproject.toml"):
            if (folder / name).exists():
                return folder / name
        if folder == checkout or checkout not in folder.parents:
            return None
        folder = folder.parent
