"""Unit tests for version sync checker and release safety invariants (Task 10.1)."""

from __future__ import annotations

import pytest
from scripts.version_sync_check import check_retired_tags, check_version_sync


def test_version_sync_check_passes() -> None:
    version = check_version_sync()
    assert version is not None
    assert len(version) > 0


def test_retired_tags_raises_error() -> None:
    with pytest.raises(ValueError, match="retired tag denylist"):
        check_retired_tags("v0.1.0-legacy")
