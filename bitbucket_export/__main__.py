"""Allow ``python -m bitbucket_export ...``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
