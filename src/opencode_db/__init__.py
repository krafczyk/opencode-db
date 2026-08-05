"""Versioned public contract for the OpenCode database cleanup CLI.

The package exposes the version constants consumed by callers and deliberately
does not open, discover, copy, or mutate databases at import time.
"""

from __future__ import annotations

__version__ = "0.1.0"
"""The creating tool version recorded by versioned protocol artifacts."""

RESULT_SCHEMA_VERSION = 1
"""The closed machine-result schema version emitted by this package."""
