"""Make the bundle variant Smithery accepts from the standard .mcpb.

Usage: uv run python scripts/smithery_manifest.py <input.mcpb> <output.mcpb>

Smithery takes python, node, binary or bun bundles and wants each tool's input schema, so this
marks the bundle python, starts the published package through uvx, and fills in the real schemas.
Smithery refuses releases to an existing server, so the bundle runs @latest: a listing stays
current without another release.
`mcpb pack` refuses those schemas, so the manifest is rewritten inside the finished archive.
"""

import asyncio
import json
import sys
import zipfile

from audial_mcp import server


async def tools() -> list[dict]:
    listed = await server.mcp.list_tools()
    listed = getattr(listed, "tools", listed)
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": getattr(tool, "input_schema", None) or tool.inputSchema,
        }
        for tool in listed
    ]


def main() -> None:
    source, target = sys.argv[1:]
    with (
        zipfile.ZipFile(source) as bundle,
        zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for item in bundle.infolist():
            data = bundle.read(item)
            if item.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["server"]["type"] = "python"
                manifest["server"]["mcp_config"]["command"] = "uvx"
                manifest["server"]["mcp_config"]["args"] = ["audial-mcp@latest"]
                manifest["tools"] = asyncio.run(tools())
                data = json.dumps(manifest, indent=2)
            out.writestr(item, data)


if __name__ == "__main__":
    main()
