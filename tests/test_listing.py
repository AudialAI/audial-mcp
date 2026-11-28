"""The listing packs in docs/ hold what both portals ask for; this checks their limits.

docs/LISTING-OPENAI.md carries the plugin package's `plugin.json` and `mcp.json`, and
docs/LISTING-CLAUDE.md the connector listing, each as a JSON block, so they can be pasted as
they are. The limits are the portals' own (OpenAI plugins/deploy/submission; Claude
connectors/building/submission and the Software Directory Policy), checked 2026-11-27.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import audial_mcp.server as server_mod
from audial_mcp import remote as remote_mode

DOCS = Path(__file__).resolve().parent.parent / "docs"
MCP_URL = "https://mcp.audialmusic.ai/mcp"
# Claude's directory policy 4B excludes music and voice generation, so the Claude listing points
# at the endpoint without generate_music and text2vox (Zach, 2026-11-28).
CLAUDE_URL = "https://mcp.audialmusic.ai/claude/mcp"

# Nothing a listing says may advertise pricing, subscriptions, trials, discounts or promotions.
SALES = re.compile(
    r"subscri|upgrad|pricing|\bprices?\b|checkout|\btrials?\b|\bfree\b|discount|promo|"
    r"top[- ]?up|\bplans?\b|\$\d",
    re.IGNORECASE,
)


def json_blocks(name: str) -> list[dict]:
    text = (DOCS / name).read_text()
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.S)]


def block_with(name: str, key: str) -> dict:
    [found] = [b for b in json_blocks(name) if key in b]
    return found


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    return remote_mode.remote_settings(
        host="127.0.0.1",
        port=8000,
        public_url="https://mcp.audialmusic.ai",
        authorization_server="https://api.audialmusic.ai",
        env={"AUDIAL_MCP_WORK_DIR": str(tmp_path_factory.mktemp("work"))},
    )


@pytest.fixture(scope="module")
def remote_tools(settings):
    server = server_mod._remote_server(settings)
    return {t.name for t in server._tool_manager.list_tools()}


@pytest.fixture(scope="module")
def claude_tools(settings):
    server = server_mod._remote_server(settings, omit=server_mod.CLAUDE_OMITTED_TOOLS)
    return {t.name for t in server._tool_manager.list_tools()}


# --- OpenAI -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plugin():
    return block_with("LISTING-OPENAI.md", "extensions")


def test_the_openai_listing_text_fits_and_sells_nothing(plugin):
    face = plugin["extensions"]["com.openai"]["interface"]
    assert face["displayName"] == "Audial" and plugin["name"] == "audial"
    assert len(face["shortDescription"]) <= 30
    assert len(face["longDescription"]) <= 4000
    assert len(face["developerName"]) <= 80 and face["category"]
    for text in (face["shortDescription"], face["longDescription"], *face["defaultPrompt"]):
        assert not SALES.search(text), SALES.search(text).group(0)
    assert len(face["defaultPrompt"]) <= 3
    assert all(len(p) <= 128 for p in face["defaultPrompt"])
    for key in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        assert face[key].startswith("https://audialmusic.ai") and len(face[key]) <= 1024
    for key in ("brandColor", "brandColorDark"):
        assert re.fullmatch(r"#[0-9a-f]{6}", face[key]), key
    assert face["logo"] == "./assets/logo.png"


def test_the_openai_test_cases_are_five_and_three_and_name_real_tools(plugin, remote_tools):
    cases = plugin["extensions"]["com.openai"]["review"]["test_cases"]
    assert len(cases["positive"]) == 5 and len(cases["negative"]) == 3
    for case in cases["positive"]:
        assert case["description"] and case["prompt"] and case["expected_behavior"]
        named = {t.strip() for t in case["tools_triggered"].split(",")}
        assert named and named <= remote_tools, named - remote_tools
    for case in cases["negative"]:
        assert case["description"] and case["prompt"]


def test_the_openai_countries_are_every_supported_one_but_india(plugin):
    countries = plugin["extensions"]["com.openai"]["publication"]["countries"]
    assert "IN" not in countries
    assert countries == sorted(set(countries))
    assert all(re.fullmatch(r"[A-Z]{2}", c) for c in countries)
    # OpenAI's list of supported countries and territories had 208 entries on 2026-11-27.
    assert len(countries) == 207 and {"US", "GB", "DE", "JP", "BR"} <= set(countries)
    assert plugin["extensions"]["com.openai"]["publication"]["release_notes"]


def test_the_openai_package_points_at_the_hosted_server_without_headers():
    config = block_with("LISTING-OPENAI.md", "mcpServers")
    [server] = config["mcpServers"].values()
    assert server["url"] == MCP_URL and server["type"] == "streamable-http"
    assert "headers" not in server


# --- Claude -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def connector():
    return block_with("LISTING-CLAUDE.md", "one_liner")


def test_the_claude_listing_fits_the_portal(connector):
    assert connector["server_url"] == CLAUDE_URL
    assert CLAUDE_URL == f"https://mcp.audialmusic.ai{remote_mode.CLAUDE_MCP_PATH}"
    assert len(connector["name"]) <= 100 and len(connector["one_liner"]) <= 200
    assert len(connector["description"]) <= 2000
    assert 1 <= len(connector["categories"]) <= 5
    assert connector["slug"] == "audial"
    for key in ("documentation_url", "privacy_policy_url"):
        assert connector[key].startswith("https://audialmusic.ai")
    # Policy 3E: at least three working examples of prompts.
    assert len(connector["example_prompts"]) >= 3
    for text in (connector["one_liner"], connector["description"]):
        assert not SALES.search(text), SALES.search(text).group(0)


# What the Claude endpoint leaves out, in words a listing might use for it.
GENERATION = re.compile(
    r"music model|generat\w* (?:\w+ )?(?:music|songs?|beats?|tracks?|vocals?)|\bsings?\b|\bsung\b|"
    r"\bsinging\b|\bvoice|text-to-speech|\bmake an? .*\bbeat\b|generate_music|text2vox",
    re.IGNORECASE,
)


def test_the_claude_listing_offers_only_what_the_claude_endpoint_serves(connector, claude_tools):
    assert claude_tools.isdisjoint({"generate_music", "text2vox"})
    texts = (connector["one_liner"], connector["description"], *connector["example_prompts"])
    for text in texts:
        assert not GENERATION.search(text), GENERATION.search(text).group(0)
    described = connector["description"].lower()
    for kept in ("stems", "master", "sample pack", "midi", "preset", "analyse"):
        assert kept in described, kept


def test_the_claude_pack_names_every_tool_on_both_sides_of_the_line(claude_tools):
    text = (DOCS / "LISTING-CLAUDE.md").read_text()
    for name in claude_tools | {"generate_music", "text2vox"}:
        assert f"`{name}`" in text, name
