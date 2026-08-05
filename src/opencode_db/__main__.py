"""Module entry point for ``python -m opencode_db``.

The module delegates to :func:`opencode_db.cli.main`; it returns that command's
exit status and has no database side effects of its own.
"""

from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
