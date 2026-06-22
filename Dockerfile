# recall. — Docker image
# Minimal Python image for the MCP server.
# LM Studio must be running on the host (port 1234).
#
# Build:   docker build -t recall-memory .
# Run:     docker run -i --rm --network=host recall-memory
# Compose: docker compose up -d

FROM python:3.11-slim

WORKDIR /app

# Install recall-memory from GitHub (or COPY from local)
RUN pip install --no-cache-dir git+https://github.com/Jnocode/recall-memory.git

# Default data directory (mount a volume here for persistence)
ENV DATA_DIR=/data
ENV EMBED_BASE_URL=http://host.docker.internal:1234
ENV RECALL_DB_PATH=/data/recall_p0.db

VOLUME ["/data"]

# MCP stdio server
ENTRYPOINT ["python", "-m", "recall.recall_mcp"]
