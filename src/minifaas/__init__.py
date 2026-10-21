"""miniFaaS: one container that serves a git repository's Functions Framework functions.

This package is imported by two interpreters. The supervisor runs it with the image's own
dependencies; the worker and discovery run it inside a release venv, which only has the
function's dependencies plus functions-framework. So this module, `worker` and `discover`
import nothing outside the standard library and Flask.
"""

from __future__ import annotations

__version__ = "0.1.0"
