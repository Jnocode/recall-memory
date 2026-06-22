# recall. — Constants / Config
# Single source of truth for shared defaults.
# Supports env var overrides for Docker deployment.

import os

# Data directory for persistent storage.
# In Docker, mount a volume here and set DATA_DIR.
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Default database path. Override with RECALL_DB_PATH env var.
DEFAULT_DB_PATH = os.environ.get("RECALL_DB_PATH", os.path.join(DATA_DIR, "recall_p0.db"))
