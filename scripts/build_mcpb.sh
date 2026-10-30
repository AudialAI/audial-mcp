#!/usr/bin/env bash
# Packs bundle/ into dist/audial-mcp-<version>.mcpb. The bundle holds no code of its own: the host
# installs audial-mcp from PyPI through uv, so the matching version must be published first.
set -euo pipefail
cd "$(dirname "$0")/.."
version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
mkdir -p dist
npx -y @anthropic-ai/mcpb@2.1.2 validate bundle/manifest.json
npx -y @anthropic-ai/mcpb@2.1.2 pack bundle "dist/audial-mcp-${version}.mcpb"
