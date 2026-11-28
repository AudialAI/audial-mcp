# The hosted remote server (https://mcp.audialmusic.ai), built by DigitalOcean App Platform from
# .do/app-mcp.yaml. Unlike the root Dockerfile (a stdio image for directories to list the tools),
# this installs the exact versions in uv.lock and serves streamable HTTP.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv==0.11.16

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# --locked: refuse to build if uv.lock does not match pyproject.toml.
RUN uv sync --locked --no-dev --no-editable && rm -rf /root/.cache

# The work folder holds callers' inputs and results; nothing else may read it.
RUN useradd --create-home --uid 10001 audial \
    && mkdir -p /tmp/audial-mcp \
    && chown audial /tmp/audial-mcp \
    && chmod 700 /tmp/audial-mcp
USER audial

ENV PATH="/app/.venv/bin:$PATH" \
    AUDIAL_MCP_HOST=0.0.0.0 \
    PORT=8080 \
    AUDIAL_MCP_WORK_DIR=/tmp/audial-mcp
EXPOSE 8080
CMD ["audial-mcp", "--http"]
