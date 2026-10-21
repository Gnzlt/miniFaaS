"""`python -m minifaas` runs the supervisor: the container's entrypoint."""

from __future__ import annotations

from minifaas.supervisor import main

if __name__ == "__main__":
    raise SystemExit(main())
