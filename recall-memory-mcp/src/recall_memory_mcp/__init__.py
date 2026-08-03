"""recall-memory-mcp — cross-client shared memory over the Model Context Protocol.

Single version source (task 2.3): ``pyproject.toml`` reads ``__version__``
through ``[tool.setuptools.dynamic]``, so this literal is the only place the
version is written.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Compatible official SDK line for the transport layer (task 2.2, design 6.1).
# Phase 2 code must not import it; phases 3+ pin against this range.
MCP_SDK_REQUIREMENT = "mcp>=2.0,<2.1"

# Distribution / import names verified against PyPI in task 0.6.
DISTRIBUTION_NAME = "recall-memory-mcp"
IMPORT_PACKAGE = "recall_memory_mcp"

__all__ = [
    "DISTRIBUTION_NAME",
    "IMPORT_PACKAGE",
    "MCP_SDK_REQUIREMENT",
    "__version__",
]
