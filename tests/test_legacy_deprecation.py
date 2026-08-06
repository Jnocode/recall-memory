"""Deprecation tests for legacy recall_mcp.py entry point (Task 9.1)."""

from __future__ import annotations

import pytest
import warnings


def test_legacy_recall_mcp_deprecation_warning() -> None:
    with pytest.deprecated_call(match="src/recall/recall_mcp.py is deprecated"):
        import importlib
        import recall.recall_mcp
        importlib.reload(recall.recall_mcp)
