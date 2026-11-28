"""The hosted server's DigitalOcean App Platform spec (.do/app-mcp.yaml) and its image.

The app is created from this file once OAuth is live (`doctl apps create --spec
.do/app-mcp.yaml`), so it has to be right before anyone runs it: the origin the listings name
cannot change once published.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from audial_mcp import remote as remote_mode

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / ".do" / "app-mcp.yaml"
ORIGIN = "https://mcp.audialmusic.ai"
SOURCE = "\n".join(p.read_text() for p in (ROOT / "src" / "audial_mcp").glob("*.py"))


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load(SPEC_PATH.read_text())


@pytest.fixture(scope="module")
def service(spec):
    [only] = spec["services"]
    return only


def env_of(service) -> dict[str, str]:
    return {e["key"]: e.get("value") for e in service.get("envs", [])}


def test_one_always_on_basic_instance(spec, service):
    assert service["instance_count"] == 1 and "autoscaling" not in service
    # Basic tier, 1 vCPU and 1 GB: $10 a month (DigitalOcean's price list, 2026-11-27).
    assert service["instance_size_slug"] == "apps-s-1vcpu-1gb-fixed"
    assert spec["region"] == "nyc"  # beside the API (audial-api-prod)
    assert [d["domain"] for d in spec["domains"] if d["type"] == "PRIMARY"] == [
        ORIGIN.removeprefix("https://")
    ]


def test_the_health_check_is_the_server_s(service):
    check = service["health_check"]
    assert check["http_path"] == remote_mode.HEALTH_PATH
    assert check["port"] == service["http_port"]


def test_the_environment_gives_the_settings_the_listings_need(service, tmp_path):
    env = env_of(service)
    settings = remote_mode.remote_settings(
        host=None,
        port=None,
        public_url=None,
        authorization_server=None,
        env={k: v for k, v in env.items() if v is not None},
    )
    assert settings.public_url == ORIGIN
    assert settings.authorization_server == "https://api.audialmusic.ai"
    assert (settings.host, settings.port) == ("0.0.0.0", service["http_port"])
    assert settings.results_ttl_s == 24 * 60 * 60
    # ChatGPT ends a tool call after about a minute; App Platform ends a request at 100 s.
    assert settings.wait_s <= 45
    # App Platform's disk is 4 GiB and the container is replaced when it fills.
    assert settings.max_results_bytes <= 3 * 1024**3
    assert str(settings.work_dir).startswith("/tmp/")


def test_every_variable_is_one_the_server_reads(service):
    for key in env_of(service):
        assert f'"{key}"' in SOURCE, key


def test_no_secret_value_is_committed(service):
    for entry in service.get("envs", []):
        if entry.get("type") == "SECRET" or re.search(r"TOKEN|SECRET|KEY|PASSWORD", entry["key"]):
            assert not entry.get("value"), entry["key"]
    # Each caller brings their own key; the server must never hold one.
    assert "AUDIAL_API_KEY" not in env_of(service)


def test_the_image_is_pinned_and_built_from_this_repo_with_the_locked_versions(service):
    # App Platform has no GitHub access to AudialAI: it runs an image pushed to Docker Hub,
    # pinned to a commit like the Audial API (a reused tag can keep serving an old build).
    image = service["image"]
    assert (image["registry_type"], image["registry"], image["repository"]) == (
        "DOCKER_HUB",
        "zfarrell13",
        "audial-mcp-remote",
    )
    assert image["tag"] not in ("", "latest")
    assert "github" not in service
    text = (ROOT / ".do" / "remote.Dockerfile").read_text()
    assert "uv sync --locked --no-dev" in text  # uv.lock, not whatever PyPI has that day
    assert re.search(r"^USER \w+", text, re.M)  # not root
    assert re.search(r'^CMD \["audial-mcp", "--http"\]', text, re.M)
    assert f"EXPOSE {service['http_port']}" in text
