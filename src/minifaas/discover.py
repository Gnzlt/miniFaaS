"""Import a release's entry module in a throwaway process and write down what it serves.

Run as `python -m minifaas.discover <source_dir> <module> <report.json>` with the release's
own interpreter. It only reads what the standard decorators already record:

- Firebase (`firebase_functions`) puts a `__firebase_endpoint__` on every decorated function.
  That is how `firebase deploy` finds the functions, their schedules and their secrets, and
  it is the only place an in-code schedule exists in the Python ecosystem.
- Functions Framework's `@functions_framework.http` registers the function by name.

Everything a Firebase decorator produces is itself a Functions Framework HTTP handler, so
the worker serves both kinds the same way. The report goes to a file because the module is
free to print whatever it likes.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

#: Firebase triggers miniFaaS has no event source for.
_UNSUPPORTED_FIREBASE = ("eventTrigger", "blockingTrigger", "taskQueueTrigger")


def _plain(value: Any) -> Any:
    """Resolve a Firebase option to a plain value: a Sentinel means unset, a param is read now."""
    if value is None or type(value).__name__ == "Sentinel":
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value"):
        return value.value
    return value


def _firebase(name: str, endpoint: Any) -> tuple[dict[str, Any] | None, str | None]:
    secrets = _plain(getattr(endpoint, "secretEnvironmentVariables", None)) or []
    spec: dict[str, Any] = {
        "name": name,
        "source": "firebase",
        "secrets": sorted({item["key"] for item in secrets}),
        "timeout": _plain(getattr(endpoint, "timeoutSeconds", None)),
    }

    schedule = getattr(endpoint, "scheduleTrigger", None)
    if schedule:
        spec |= {
            "trigger": "schedule",
            "schedule": _plain(schedule.get("schedule")),
            "timezone": _plain(schedule.get("timeZone")),
        }
        return spec, None
    if getattr(endpoint, "httpsTrigger", None) is not None or getattr(endpoint, "callableTrigger", None):
        return spec | {"trigger": "http"}, None
    for kind in _UNSUPPORTED_FIREBASE:
        if getattr(endpoint, kind, None):
            return None, kind
    return None, "unknown trigger"


def discover(source_dir: Path, module_name: str) -> dict[str, Any]:
    sys.path.insert(0, str(source_dir))
    module = importlib.import_module(module_name)

    functions: dict[str, dict[str, Any]] = {}
    unsupported: list[dict[str, str]] = []

    for name, obj in vars(module).items():
        endpoint = getattr(obj, "__firebase_endpoint__", None)
        if endpoint is None or not callable(obj):
            continue
        spec, reason = _firebase(name, endpoint)
        if spec:
            functions[name] = spec
        else:
            unsupported.append({"name": name, "reason": f"Firebase {reason}"})

    registry = sys.modules.get("functions_framework._function_registry")
    for name, signature in getattr(registry, "REGISTRY_MAP", {}).items():
        if name in functions or not callable(getattr(module, name, None)):
            continue
        if signature == "http":
            functions[name] = {"name": name, "source": "functions-framework", "trigger": "http", "secrets": []}
        else:
            unsupported.append({"name": name, "reason": f"{signature} signature"})

    return {"functions": sorted(functions.values(), key=lambda f: f["name"]), "unsupported": unsupported}


def main() -> int:
    source_dir, module_name, report = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    os.chdir(source_dir)
    result = discover(source_dir, module_name)
    report.write_text(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
