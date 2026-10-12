# Minimal image so directories (Glama) can start the server and introspect its
# tools. Users don't need this: `uvx audial-mcp` is the supported install.
FROM python:3.12-slim
RUN pip install --no-cache-dir audial-mcp==0.1.1
# Credentials are supplied by the MCP client at runtime (AUDIAL_USER_ID,
# AUDIAL_API_KEY); the server starts and lists its tools without them.
CMD ["audial-mcp"]
