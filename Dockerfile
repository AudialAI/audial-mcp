# Minimal image so directories (Glama) can start the server and introspect its
# tools. Users don't need this: `uvx audial-mcp` is the supported install.
# Built from this checkout, so what a directory sees is always the current code.
FROM python:3.12-slim
COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
RUN pip install --no-cache-dir /app
# Credentials are supplied by the MCP client at runtime (AUDIAL_USER_ID,
# AUDIAL_API_KEY); the server starts and lists its tools without them.
CMD ["audial-mcp"]
