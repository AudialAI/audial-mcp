# audial-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `audial-mcp` 0.1.0, a local stdio MCP server wrapping the Audial SDK, published to PyPI, the official MCP registry, the Claude directory (as a plugin), with website documentation and a Cursor install button.

**Architecture:** Python package built on `mcp>=2.2` (`MCPServer`); ten async tools that validate inputs, create a job folder under `~/Audial`, run the blocking SDK call in a worker thread while a heartbeat reports progress, and return a uniform structured result. Distribution is metadata pointing at the PyPI package (registry `server.json`, Claude plugin `.mcp.json`, website config blocks).

**Tech Stack:** Python 3.10+, `mcp` 2.2.x, `audial-sdk` 1.2.2, `anyio`, `pydantic`, `hatchling`, `pytest` + `pytest-anyio`, `uv`, GitHub Actions, `mcp-publisher`, Claude Code `claude plugin validate`, React/TypeScript (website).

**Spec:** `docs/2026-09-29-audial-mcp-design.md`

## Global Constraints

- Python `>=3.10`; dependencies pinned as `mcp>=2.2,<3`, `audial-sdk>=1.2.2`, `anyio>=4`, `pydantic>=2`.
- Package name `audial-mcp`, import name `audial_mcp`, console script `audial-mcp`, server name `audial`, version `0.1.0`.
- Tool names exactly: `stem_split`, `analyze`, `segment`, `master`, `generate_samples`, `generate_midi`, `generate_music`, `sound2vital`, `text2vox`, `list_results`.
- Environment variables exactly: `AUDIAL_USER_ID`, `AUDIAL_API_KEY`, `AUDIAL_RESULTS_DIR` (default `~/Audial`), `AUDIAL_API_BASE_URL` (optional), `AUDIAL_JOB_TIMEOUT_S` (default `900`).
- Results path: `<AUDIAL_RESULTS_DIR>/<tool>/<YYYY-MM-DD_HHMMSS>_<slug>/`.
- Every user-visible failure is a `ToolError` (`from mcp.server.mcpserver.exceptions import ToolError`); never let a bare exception escape a tool.
- Credentials never appear in results, INFO logs, or error text.
- Documentation and metadata never name the underlying music engine or upstream models; say "Audial music model" (Zach's standing rule).
- Nothing is pushed to GitHub, PyPI, the registry or the website without the explicit approval step named in each deployment task.
- All Python code passes `ruff check` and `ruff format --check`; all tests pass with `pytest -q`.
- Git: commit after every task with the attribution lines the session requires.

---

## File structure

```
audial-mcp/
├── pyproject.toml              package metadata, deps, scripts, ruff/pytest config
├── README.md                   landing page + `mcp-name:` registry marker
├── LICENSE                     MIT
├── server.json                 official MCP registry descriptor
├── .gitignore
├── .github/workflows/ci.yml    ruff + pytest on push/PR
├── .github/workflows/publish.yml  build + PyPI upload on tag v*
├── src/audial_mcp/
│   ├── __init__.py             __version__
│   ├── __main__.py             `python -m audial_mcp`
│   ├── config.py               Settings + load_settings + ConfigError
│   ├── results.py              slugs, job folders, inventory, list_results
│   ├── jobs.py                 run_job (thread + heartbeat + timeout)
│   ├── errors.py               SDK exception → ToolError mapping
│   ├── validation.py           audio/midi/json path checks
│   └── server.py               MCPServer, tools, main()
├── tests/
│   ├── conftest.py             anyio backend, settings + results_dir fixtures, fake SDK
│   ├── test_config.py
│   ├── test_results.py
│   ├── test_jobs.py
│   ├── test_validation.py
│   ├── test_errors.py
│   └── test_server.py          in-memory Client tests per tool
└── docs/
    ├── 2026-09-29-audial-mcp-design.md
    ├── 2026-09-29-audial-mcp-implementation-plan.md
    ├── manual-e2e-checklist.md
    └── release-checklist.md
```

Sibling deliverables (separate repos / existing repos):

- `audial-sdk` 1.2.2 (Task 0).
- `AudialAI/audial-claude-plugin` (Task 10).
- `audial-fe-re` website MCP tab (Task 11).

---

### Task 0: SDK 1.2.2 — configurable waits for music and MIDI generation

**Files:**
- Modify: `audial-sdk/audial/functions/generate_music.py:161` (hard-coded `max_wait = 300`)
- Modify: `audial-sdk/audial/functions/midi.py:143` (hard-coded `max_processing_time = 5 * 60`)
- Modify: `audial-sdk/setup.py` (version `1.2.2`)
- Test: `audial-sdk/tests/test_generate_music_wait.py`

**Interfaces:**
- Produces: `generate_music(..., max_wait: float = 900, poll_interval: float = 5)` and `generate_midi(..., max_wait: float = 900)`; both raise `AudialAPIError("... timed out after {max_wait}s")` on expiry.

- [ ] **Step 1: Write the failing test**

```python
# audial-sdk/tests/test_generate_music_wait.py
import inspect
from audial.functions.generate_music import generate_music
from audial.functions.midi import generate_midi


def test_generate_music_exposes_max_wait():
    params = inspect.signature(generate_music).parameters
    assert params["max_wait"].default == 900
    assert params["poll_interval"].default == 5


def test_generate_midi_exposes_max_wait():
    params = inspect.signature(generate_midi).parameters
    assert params["max_wait"].default == 900
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd audial-sdk && .venv/bin/pytest -q tests/test_generate_music_wait.py`
Expected: FAIL with `KeyError: 'max_wait'`

- [ ] **Step 3: Implement**

In `generate_music.py`, add to the signature (after `api_key: str = None`):

```python
max_wait: float = (900,)
poll_interval: float = (5,)
```

and replace the two local assignments (`max_wait = 300  # 5 minutes (matching frontend)` and `poll_interval = 5`) with uses of the parameters. Update the docstring:

```
        max_wait: Seconds to wait for the job before raising AudialAPIError. Default 900
            (cold-started workers can take several minutes).
        poll_interval: Seconds between status checks. Default 5.
```

In `midi.py`, add `max_wait: float = 900` to `generate_midi`'s signature and replace `max_processing_time = 5 * 60` with `max_processing_time = max_wait`; document it the same way.

Bump `setup.py` to `version="1.2.2"`.

- [ ] **Step 4: Run the full SDK suite**

Run: `cd audial-sdk && .venv/bin/pytest -q`
Expected: all pass (23 existing + 2 new)

- [ ] **Step 5: Commit, tag, publish (APPROVAL: publishing to PyPI is outward-facing; confirm with Zach before `twine upload`)**

```bash
cd audial-sdk
git add -A audial tests setup.py
git commit -m "1.2.2: configurable max_wait for generate_music and generate_midi"
git tag -a v1.2.2 -m "audial-sdk 1.2.2"
git push origin main v1.2.2
rm -rf build dist/audial_sdk-1.2.2* && .venv/bin/python -m build -q
.venv/bin/twine upload --non-interactive -u __token__ -p "$PYPI_TOKEN" dist/audial_sdk-1.2.2*
```

Verify: `curl -s https://pypi.org/pypi/audial-sdk/json | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])'` prints `1.2.2`.

---

### Task 1: Repository scaffold

**Files:**
- Create: `pyproject.toml`, `README.md`, `LICENSE`, `.gitignore`, `src/audial_mcp/__init__.py`, `src/audial_mcp/__main__.py`, `tests/conftest.py`

**Interfaces:**
- Produces: importable `audial_mcp` with `__version__ = "0.1.0"`; `uv run pytest` works.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "audial-mcp"
version = "0.1.0"
description = "MCP server for Audial: stem splitting, mastering, MIDI, music generation, resynthesis and vocal synthesis from any MCP client."
readme = "README.md"
license = "MIT"
requires-python = ">=3.10"
authors = [{ name = "Audial", email = "contact@audialmusic.ai" }]
keywords = ["mcp", "audio", "music", "stems", "audial", "model-context-protocol"]
classifiers = [
  "Programming Language :: Python :: 3",
  "License :: OSI Approved :: MIT License",
  "Topic :: Multimedia :: Sound/Audio",
]
dependencies = [
  "mcp>=2.2,<3",
  "audial-sdk>=1.2.2",
  "anyio>=4",
  "pydantic>=2",
]

[project.urls]
Homepage = "https://audialmusic.ai"
Documentation = "https://audialmusic.ai/resources/api-reference"
Repository = "https://github.com/AudialAI/audial-mcp"

[project.scripts]
audial-mcp = "audial_mcp.server:main"

[dependency-groups]
dev = ["pytest>=8", "pytest-anyio>=0.0.0; python_version < '0'", "anyio[trio]>=4", "ruff>=0.6", "build>=1.2"]

[tool.hatch.build.targets.wheel]
packages = ["src/audial_mcp"]

[tool.pytest.ini_options]
testpaths = ["tests"]
anyio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

(Drop the placeholder `pytest-anyio` line; `anyio`'s bundled pytest plugin is what runs `@pytest.mark.anyio` tests. The dev group is: `pytest>=8`, `anyio>=4`, `ruff>=0.6`, `build>=1.2`.)

- [ ] **Step 2: Write `README.md`** (this text is also the PyPI page and the registry validator reads the marker line)

```markdown
# audial-mcp

Run Audial's hosted audio tools from any MCP client: split stems, analyze and segment audio,
master tracks, build sample packs, convert audio to MIDI, generate music, turn a one-shot into
an editable Audial Synth preset, and sing lyrics in a reference voice.

mcp-name: io.github.AudialAI/audial-mcp

## Install

You need an Audial account (user id + API key from https://audialmusic.ai) and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/).

**Claude Code**

```bash
claude mcp add audial \
  -e AUDIAL_USER_ID=your-user-id -e AUDIAL_API_KEY=your-api-key -e AUDIAL_RESULTS_DIR=~/Audial \
  -- uvx audial-mcp
```

**Claude Desktop / Cursor / any client with a JSON config**

```json
{
  "mcpServers": {
    "audial": {
      "command": "uvx",
      "args": ["audial-mcp"],
      "env": {
        "AUDIAL_USER_ID": "your-user-id",
        "AUDIAL_API_KEY": "your-api-key",
        "AUDIAL_RESULTS_DIR": "~/Audial"
      }
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `stem_split` | Split a track into vocals, drums, bass, other (optionally retime / rekey) |
| `analyze` | BPM, key and other characteristics |
| `segment` | Sections and component analysis |
| `master` | Mastering, optionally matched to a reference |
| `generate_samples` | Sample pack from a track |
| `generate_midi` | Audio to MIDI |
| `generate_music` | Text to music, covers, remixes, extraction, completion with the Audial music model |
| `sound2vital` | One-shot → editable Audial Synth (Vital) preset |
| `text2vox` | Lyrics + reference voice → sung vocal and MIDI |
| `list_results` | Browse previous results in your results folder |

## What leaves your machine

Audio and text you pass to a tool are uploaded to Audial's API (https://api.audialmusic.ai)
over HTTPS and processed on Audial's servers; results are downloaded into
`AUDIAL_RESULTS_DIR` (default `~/Audial`). Nothing else is read or sent. Some tools require
an active Audial subscription; the tool tells you when that is the case.

## Configuration

| Variable | Required | Default |
|---|---|---|
| `AUDIAL_USER_ID` | yes | |
| `AUDIAL_API_KEY` | yes | |
| `AUDIAL_RESULTS_DIR` | no | `~/Audial` |
| `AUDIAL_JOB_TIMEOUT_S` | no | `900` |
| `AUDIAL_API_BASE_URL` | no | production API |

## License

MIT. Source: https://github.com/AudialAI/audial-mcp
```

- [ ] **Step 3: Write `LICENSE`** (MIT, `Copyright (c) 2026 Audial`), `.gitignore` (`.venv/`, `dist/`, `build/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.ruff_cache/`, `uv.lock` is committed), `src/audial_mcp/__init__.py`:

```python
"""audial-mcp: a local MCP server for the Audial audio tools."""

__version__ = "0.1.0"
```

`src/audial_mcp/__main__.py`:

```python
from audial_mcp.server import main

if __name__ == "__main__":
    main()
```

`tests/conftest.py`:

```python
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 4: Verify the environment resolves**

Run: `cd audial-mcp && uv sync --group dev && uv run python -c "import audial_mcp, mcp; print(audial_mcp.__version__, mcp.__version__ if hasattr(mcp,'__version__') else 'mcp ok')" && uv run pytest -q`
Expected: prints `0.1.0 …`; pytest reports `no tests ran` (exit 5 is fine at this step).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "scaffold: package metadata, README, license"
```

---

### Task 2: Settings from the environment

**Files:**
- Create: `src/audial_mcp/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings(user_id, api_key, results_dir: Path, api_base_url, job_timeout_s)`, `load_settings(env: Mapping[str, str] = os.environ) -> Settings`, `ConfigError(Exception)`, `Settings.require_credentials()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from audial_mcp.config import ConfigError, Settings, load_settings


def test_defaults_when_only_credentials_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = load_settings({"AUDIAL_USER_ID": "u1", "AUDIAL_API_KEY": "k1"})
    assert s.user_id == "u1" and s.api_key == "k1"
    assert s.results_dir == tmp_path / "Audial"
    assert s.api_base_url is None
    assert s.job_timeout_s == 900


def test_results_dir_expands_tilde_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = load_settings({"AUDIAL_RESULTS_DIR": "~/Music/AudialOut"})
    assert s.results_dir == tmp_path / "Music" / "AudialOut"


def test_timeout_parses_int_and_rejects_garbage():
    assert load_settings({"AUDIAL_JOB_TIMEOUT_S": "120"}).job_timeout_s == 120
    with pytest.raises(ConfigError, match="AUDIAL_JOB_TIMEOUT_S"):
        load_settings({"AUDIAL_JOB_TIMEOUT_S": "soon"})


def test_require_credentials_message_names_both_variables():
    s = load_settings({})
    with pytest.raises(ConfigError) as exc:
        s.require_credentials()
    assert "AUDIAL_USER_ID" in str(exc.value) and "AUDIAL_API_KEY" in str(exc.value)
    assert "audialmusic.ai" in str(exc.value)


def test_credentials_present_passes():
    Settings(
        user_id="u", api_key="k", results_dir=Path("/tmp"), api_base_url=None, job_timeout_s=1
    ).require_credentials()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_config.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'audial_mcp.config'`

- [ ] **Step 3: Implement `config.py`**

```python
"""Environment-driven settings for the audial MCP server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RESULTS_DIR = "~/Audial"
DEFAULT_JOB_TIMEOUT_S = 900

CREDENTIALS_HELP = (
    "AUDIAL_USER_ID and AUDIAL_API_KEY are not set. Add both to the `audial` server's env in "
    "your MCP client config (for Claude Code: `claude mcp add audial -e AUDIAL_USER_ID=... "
    "-e AUDIAL_API_KEY=... -- uvx audial-mcp`). Get them from your dashboard at "
    "https://audialmusic.ai, then restart the client."
)


class ConfigError(Exception):
    """Raised when the environment cannot be turned into usable settings."""


@dataclass(frozen=True)
class Settings:
    user_id: str | None
    api_key: str | None
    results_dir: Path
    api_base_url: str | None
    job_timeout_s: int

    def require_credentials(self) -> None:
        if not self.user_id or not self.api_key:
            raise ConfigError(CREDENTIALS_HELP)


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    raw_timeout = env.get("AUDIAL_JOB_TIMEOUT_S", str(DEFAULT_JOB_TIMEOUT_S))
    try:
        timeout = int(raw_timeout)
        if timeout <= 0:
            raise ValueError
    except ValueError:
        raise ConfigError(
            f"AUDIAL_JOB_TIMEOUT_S must be a positive integer number of seconds, got {raw_timeout!r}"
        ) from None

    results_dir = Path(
        os.path.expandvars(env.get("AUDIAL_RESULTS_DIR") or DEFAULT_RESULTS_DIR)
    ).expanduser()

    return Settings(
        user_id=(env.get("AUDIAL_USER_ID") or "").strip() or None,
        api_key=(env.get("AUDIAL_API_KEY") or "").strip() or None,
        results_dir=results_dir,
        api_base_url=(env.get("AUDIAL_API_BASE_URL") or "").strip() or None,
        job_timeout_s=timeout,
    )
```

Note: `Path.expanduser()` honours `HOME`, which the tests set.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_config.py`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "config: settings from environment with clear credential guidance"
```

---

### Task 3: Results folders and inventory

**Files:**
- Create: `src/audial_mcp/results.py`
- Test: `tests/test_results.py`

**Interfaces:**
- Produces:
  - `slugify(text: str, fallback: str = "job") -> str` (`[A-Za-z0-9_-]`, ≤ 40 chars)
  - `new_job_dir(results_dir: Path, tool: str, slug: str, now: datetime | None = None) -> Path` (creates it)
  - `FileInfo` TypedDict `{name, path, size_bytes}`; `inventory(folder: Path) -> list[FileInfo]` (recursive, sorted by relative path, skips hidden files)
  - `JobResult` TypedDict `{tool, execution_id, output_dir, files, metadata, summary}`
  - `list_jobs(results_dir: Path, tool: str | None, limit: int) -> list[JobResult]` (newest first by folder name)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_results.py
from datetime import datetime
from pathlib import Path

from audial_mcp.results import inventory, list_jobs, new_job_dir, slugify


def test_slugify_strips_and_truncates():
    assert slugify("My Demo (final).wav") == "My_Demo_final_wav"
    assert slugify("   ") == "job"
    assert len(slugify("x" * 100)) == 40


def test_new_job_dir_layout(tmp_path):
    d = new_job_dir(tmp_path, "stem_split", "demo", now=datetime(2026, 9, 29, 10, 45, 12))
    assert d == tmp_path / "stem_split" / "2026-09-29_104512_demo"
    assert d.is_dir()


def test_inventory_is_recursive_sorted_and_skips_hidden(tmp_path):
    (tmp_path / "b.wav").write_bytes(b"12")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.mid").write_bytes(b"1")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    files = inventory(tmp_path)
    assert [f["name"] for f in files] == ["b.wav", "sub/a.mid"]
    assert files[0]["size_bytes"] == 2 and Path(files[0]["path"]).is_absolute()


def test_list_jobs_newest_first_and_filtered(tmp_path):
    older = new_job_dir(tmp_path, "stem_split", "one", now=datetime(2026, 9, 1, 0, 0, 0))
    newer = new_job_dir(tmp_path, "stem_split", "two", now=datetime(2026, 9, 2, 0, 0, 0))
    other = new_job_dir(tmp_path, "analyze", "three", now=datetime(2026, 9, 3, 0, 0, 0))
    for d in (older, newer, other):
        (d / "out.wav").write_bytes(b"0")
    jobs = list_jobs(tmp_path, tool=None, limit=10)
    assert [j["output_dir"] for j in jobs] == [str(other), str(newer), str(older)]
    only_stems = list_jobs(tmp_path, tool="stem_split", limit=1)
    assert len(only_stems) == 1 and only_stems[0]["output_dir"] == str(newer)
    assert only_stems[0]["files"][0]["name"] == "out.wav"


def test_list_jobs_empty_when_dir_missing(tmp_path):
    assert list_jobs(tmp_path / "nope", tool=None, limit=5) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_results.py`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `results.py`**

```python
"""Where results live and how they are reported back to the model."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
SLUG_MAX = 40


class FileInfo(TypedDict):
    name: str
    path: str
    size_bytes: int


class JobResult(TypedDict):
    tool: str
    execution_id: str | None
    output_dir: str
    files: list[FileInfo]
    metadata: dict[str, Any]
    summary: str


def slugify(text: str, fallback: str = "job") -> str:
    slug = _SLUG_RE.sub("_", text.strip()).strip("_")[:SLUG_MAX]
    return slug or fallback


def new_job_dir(results_dir: Path, tool: str, slug: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    folder = results_dir / tool / f"{stamp}_{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def inventory(folder: Path) -> list[FileInfo]:
    if not folder.is_dir():
        return []
    files: list[FileInfo] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or any(
            part.startswith(".") for part in path.relative_to(folder).parts
        ):
            continue
        files.append(
            FileInfo(
                name=path.relative_to(folder).as_posix(),
                path=str(path.resolve()),
                size_bytes=path.stat().st_size,
            )
        )
    return files


def list_jobs(results_dir: Path, tool: str | None, limit: int) -> list[JobResult]:
    if not results_dir.is_dir():
        return []
    tools = [tool] if tool else sorted(p.name for p in results_dir.iterdir() if p.is_dir())
    jobs: list[tuple[str, JobResult]] = []
    for name in tools:
        tool_dir = results_dir / name
        if not tool_dir.is_dir():
            continue
        for job_dir in tool_dir.iterdir():
            if not job_dir.is_dir():
                continue
            files = inventory(job_dir)
            jobs.append(
                (
                    job_dir.name,
                    JobResult(
                        tool=name,
                        execution_id=None,
                        output_dir=str(job_dir),
                        files=files,
                        metadata={},
                        summary=f"{name}: {len(files)} file(s) in {job_dir}",
                    ),
                )
            )
    jobs.sort(key=lambda item: item[0], reverse=True)
    return [job for _, job in jobs[: max(limit, 0)]]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_results.py`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "results: job folders, inventory and listing"
```

---

### Task 4: Input validation

**Files:**
- Create: `src/audial_mcp/validation.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Produces: `AUDIO_EXTENSIONS`, `MIDI_EXTENSIONS`, `JSON_EXTENSIONS` frozensets; `check_file(path: str, kinds: frozenset[str], label: str) -> Path` (expands `~`, resolves, raises `ValidationError(str)` with the exact message shapes below); `ValidationError(Exception)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_validation.py
import pytest

from audial_mcp.validation import AUDIO_EXTENSIONS, MIDI_EXTENSIONS, ValidationError, check_file


def test_accepts_audio_and_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "demo.WAV"
    f.write_bytes(b"0")
    assert check_file("~/demo.WAV", AUDIO_EXTENSIONS, "audio file") == f.resolve()


def test_missing_file_message(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        check_file(str(tmp_path / "missing.wav"), AUDIO_EXTENSIONS, "audio file")


def test_directory_rejected(tmp_path):
    with pytest.raises(ValidationError, match="is a directory"):
        check_file(str(tmp_path), AUDIO_EXTENSIONS, "audio file")


def test_wrong_extension_lists_allowed(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    with pytest.raises(ValidationError) as exc:
        check_file(str(f), MIDI_EXTENSIONS, "MIDI file")
    assert ".mid" in str(exc.value) and "notes.txt" in str(exc.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_validation.py`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `validation.py`**

```python
"""Path checks that turn bad input into messages the model can act on."""

from __future__ import annotations

import os
from pathlib import Path

AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aif", ".aiff", ".flac", ".m4a", ".ogg", ".aac"})
MIDI_EXTENSIONS = frozenset({".mid", ".midi"})
JSON_EXTENSIONS = frozenset({".json"})


class ValidationError(Exception):
    """Input the user gave cannot be used; the message says why."""


def check_file(path: str, kinds: frozenset[str], label: str) -> Path:
    candidate = Path(os.path.expandvars(path)).expanduser()
    if not candidate.exists():
        raise ValidationError(f"{candidate} does not exist. Give the full path to the {label}.")
    if candidate.is_dir():
        raise ValidationError(f"{candidate} is a directory; give the path to a single {label}.")
    if candidate.suffix.lower() not in kinds:
        allowed = ", ".join(sorted(kinds))
        raise ValidationError(f"{candidate.name} is not a supported {label} (expected {allowed}).")
    return candidate.resolve()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_validation.py`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "validation: audio, MIDI and JSON path checks"
```

---

### Task 5: Job runner with heartbeat progress and timeout

**Files:**
- Create: `src/audial_mcp/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: an MCP `Context` (only `report_progress` and `info` are used; tests pass a fake).
- Produces: `async def run_job(ctx, label: str, fn: Callable[[], T], *, timeout_s: float, heartbeat_s: float = 5.0) -> T`; raises `JobTimeout(label, elapsed_s)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_jobs.py
import time

import anyio
import pytest

from audial_mcp.jobs import JobTimeout, run_job


class FakeCtx:
    def __init__(self):
        self.progress = []
        self.logs = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append((progress, total, message))

    async def info(self, message):
        self.logs.append(message)


@pytest.mark.anyio
async def test_returns_result_and_emits_heartbeats():
    ctx = FakeCtx()

    def work():
        time.sleep(0.35)
        return "done"

    result = await run_job(ctx, "stem_split", work, timeout_s=5, heartbeat_s=0.1)
    assert result == "done"
    assert len(ctx.progress) >= 2
    assert all(m.startswith("stem_split:") for _, _, m in ctx.progress)
    assert [p for p, _, _ in ctx.progress] == sorted(p for p, _, _ in ctx.progress)


@pytest.mark.anyio
async def test_timeout_raises_job_timeout():
    ctx = FakeCtx()

    def slow():
        time.sleep(0.5)
        return "late"

    with pytest.raises(JobTimeout) as exc:
        await run_job(ctx, "master", slow, timeout_s=0.15, heartbeat_s=0.05)
    assert exc.value.label == "master" and exc.value.elapsed_s >= 0.15
    await anyio.sleep(0.5)  # let the orphaned thread finish before the loop closes


@pytest.mark.anyio
async def test_exceptions_from_work_propagate():
    ctx = FakeCtx()

    def bad():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await run_job(ctx, "analyze", bad, timeout_s=1, heartbeat_s=0.05)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_jobs.py`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `jobs.py`**

```python
"""Run blocking SDK calls without freezing the MCP connection."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

import anyio

T = TypeVar("T")


class JobTimeout(Exception):
    def __init__(self, label: str, elapsed_s: float):
        super().__init__(f"{label} did not finish within {elapsed_s:.0f} s")
        self.label = label
        self.elapsed_s = elapsed_s


async def run_job(
    ctx: Any,
    label: str,
    fn: Callable[[], T],
    *,
    timeout_s: float,
    heartbeat_s: float = 5.0,
) -> T:
    """Run `fn` in a worker thread; report elapsed seconds as progress until it returns."""
    started = time.monotonic()

    async def heartbeat() -> None:
        while True:
            await anyio.sleep(heartbeat_s)
            elapsed = int(time.monotonic() - started)
            await ctx.report_progress(elapsed, message=f"{label}: {elapsed} s elapsed")

    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat)
        try:
            with anyio.fail_after(timeout_s):
                # Cancelling to_thread does not stop the thread; it finishes quietly.
                result = await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)
        except TimeoutError:
            raise JobTimeout(label, time.monotonic() - started) from None
        finally:
            tg.cancel_scope.cancel()
    return result
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_jobs.py`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "jobs: worker-thread runner with progress heartbeat and timeout"
```

---

### Task 6: Error mapping

**Files:**
- Create: `src/audial_mcp/errors.py`
- Test: `tests/test_errors.py`

**Interfaces:**
- Consumes: `audial.api.exceptions.{AudialError, AudialAuthError, AudialAPIError, SubscriptionRequiredError}`, `ConfigError`, `ValidationError`, `JobTimeout`.
- Produces: `to_tool_error(exc: BaseException, *, tool: str, execution_id: str | None = None) -> ToolError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_errors.py
from audial.api.exceptions import AudialAPIError, AudialAuthError, SubscriptionRequiredError
from mcp.server.mcpserver.exceptions import ToolError

from audial_mcp.config import ConfigError
from audial_mcp.errors import to_tool_error
from audial_mcp.jobs import JobTimeout
from audial_mcp.validation import ValidationError


def test_subscription_error_keeps_api_text():
    err = to_tool_error(
        SubscriptionRequiredError("This feature needs an active Audial subscription."),
        tool="sound2vital",
    )
    assert isinstance(err, ToolError) and "subscription" in str(err)


def test_auth_error_points_at_variables():
    err = to_tool_error(AudialAuthError("401"), tool="analyze")
    assert "AUDIAL_USER_ID" in str(err) and "AUDIAL_API_KEY" in str(err)


def test_config_and_validation_pass_through():
    assert "hint" in str(to_tool_error(ConfigError("hint"), tool="x"))
    assert "bad path" in str(to_tool_error(ValidationError("bad path"), tool="x"))


def test_timeout_mentions_execution_id():
    err = to_tool_error(JobTimeout("master", 900), tool="master", execution_id="-P2h")
    assert "900" in str(err) and "-P2h" in str(err) and "still" in str(err)


def test_generic_api_error_verbatim_and_unknown_is_generic():
    assert "quota" in str(to_tool_error(AudialAPIError("quota exceeded"), tool="x"))
    msg = str(to_tool_error(RuntimeError("secret internals sk-123"), tool="x"))
    assert "sk-123" not in msg and "x failed unexpectedly" in msg
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_errors.py`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `errors.py`**

```python
"""Turn every failure into a ToolError the model can read and act on."""

from __future__ import annotations

import logging

from audial.api.exceptions import AudialAuthError, AudialError, SubscriptionRequiredError
from mcp.server.mcpserver.exceptions import ToolError

from audial_mcp.config import ConfigError
from audial_mcp.jobs import JobTimeout
from audial_mcp.validation import ValidationError

log = logging.getLogger("audial_mcp")


def to_tool_error(exc: BaseException, *, tool: str, execution_id: str | None = None) -> ToolError:
    if isinstance(exc, (ConfigError, ValidationError, SubscriptionRequiredError)):
        return ToolError(str(exc))
    if isinstance(exc, AudialAuthError):
        return ToolError(
            "Audial rejected the credentials. Check AUDIAL_USER_ID and AUDIAL_API_KEY in the "
            "server's env and restart the client."
        )
    if isinstance(exc, JobTimeout):
        ref = f" (execution {execution_id})" if execution_id else ""
        return ToolError(
            f"{exc.label} did not finish within {exc.elapsed_s:.0f} s{ref}. The job may still "
            "complete on Audial's side; try again, or use a shorter file."
        )
    if isinstance(exc, AudialError):
        return ToolError(str(exc))
    log.exception("%s failed", tool)
    return ToolError(f"{tool} failed unexpectedly. Details were logged to the server's stderr.")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q tests/test_errors.py`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "errors: map SDK, config, validation and timeout failures to ToolError"
```

---

### Task 7: The server and its ten tools

**Files:**
- Create: `src/audial_mcp/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: everything above; `audial.stem_split`, `audial.analyze`, `audial.segment`, `audial.master`, `audial.generate_samples`, `audial.generate_midi`, `audial.generate_music`, `audial.sound2vital`, `audial.text2vox` (module-level names in the `audial` package, patched in tests).
- Produces: module-level `mcp = MCPServer("audial", ...)`; `main()`; tools returning `JobResult`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server.py
import json
from pathlib import Path

import pytest
from mcp import Client

import audial_mcp.server as server_mod
from audial_mcp.config import Settings

TOOLS = {
    "stem_split",
    "analyze",
    "segment",
    "master",
    "generate_samples",
    "generate_midi",
    "generate_music",
    "sound2vital",
    "text2vox",
    "list_results",
}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(
        user_id="u1",
        api_key="k1",
        results_dir=tmp_path / "Audial",
        api_base_url=None,
        job_timeout_s=5,
    )
    monkeypatch.setattr(server_mod, "_settings", s)
    return s


@pytest.fixture
def wav(tmp_path):
    f = tmp_path / "demo.wav"
    f.write_bytes(b"RIFF")
    return f


@pytest.fixture
async def client(settings):
    async with Client(server_mod.mcp, raise_exceptions=True) as c:
        yield c


def fake_sdk(monkeypatch, name, *, produce=("vocals.wav",), returns=None, calls=None):
    calls = calls if calls is not None else []

    def fn(*args, **kwargs):
        calls.append((args, kwargs))
        folder = Path(kwargs["results_folder"])
        for fname in produce:
            (folder / fname).write_bytes(b"0")
        print("SDK progress line that must not corrupt the protocol")
        return (
            returns
            if returns is not None
            else {"execution": {"exeId": "-P2h", "state": "completed"}}
        )

    monkeypatch.setattr(server_mod.audial, name, fn)
    return calls


@pytest.mark.anyio
async def test_lists_all_tools(client):
    tools = await client.list_tools()
    assert {t.name for t in tools.tools} == TOOLS


@pytest.mark.anyio
async def test_stem_split_maps_args_and_inventories_output(client, settings, wav, monkeypatch):
    calls = fake_sdk(monkeypatch, "stem_split", produce=("vocals.wav", "drums.wav"))
    result = await client.call_tool(
        "stem_split", {"file_path": str(wav), "stems": ["vocals", "drums"], "target_bpm": 90}
    )
    assert not result.is_error
    data = result.structured_content
    assert data["tool"] == "stem_split" and data["execution_id"] == "-P2h"
    assert Path(data["output_dir"]).parent == settings.results_dir / "stem_split"
    assert [f["name"] for f in data["files"]] == ["drums.wav", "vocals.wav"]
    (args, kwargs) = calls[0]
    assert kwargs["file_path"] == str(wav.resolve()) and kwargs["stems"] == ["vocals", "drums"]
    assert kwargs["target_bpm"] == 90 and kwargs["results_folder"] == data["output_dir"]


@pytest.mark.anyio
async def test_missing_credentials_is_a_readable_error(client, monkeypatch, wav, settings):
    monkeypatch.setattr(
        server_mod, "_settings", Settings(None, None, settings.results_dir, None, 5)
    )
    result = await client.call_tool("analyze", {"file_path": str(wav)})
    assert result.is_error and "AUDIAL_API_KEY" in result.content[0].text


@pytest.mark.anyio
async def test_bad_path_is_a_readable_error(client):
    result = await client.call_tool("analyze", {"file_path": "/nope/missing.wav"})
    assert result.is_error and "does not exist" in result.content[0].text


@pytest.mark.anyio
async def test_generate_music_uses_prompt_slug_and_no_file(client, settings, monkeypatch):
    calls = fake_sdk(
        monkeypatch,
        "generate_music",
        produce=("song.mp3",),
        returns={"execution": {"exeId": "-P2m", "generation_metadata": {"bpm": 96}}},
    )
    result = await client.call_tool(
        "generate_music", {"prompt": "upbeat country shuffle", "audio_duration": 30}
    )
    data = result.structured_content
    assert Path(data["output_dir"]).name.endswith("_upbeat_country_shuffle")
    assert data["metadata"]["execution"]["generation_metadata"]["bpm"] == 96
    assert calls[0][1]["prompt"] == "upbeat country shuffle" and calls[0][1]["max_wait"] == 5


@pytest.mark.anyio
async def test_text2vox_validates_reference_and_midi(client, tmp_path, monkeypatch):
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"0")
    mid = tmp_path / "melody.mid"
    mid.write_bytes(b"0")
    calls = fake_sdk(monkeypatch, "text2vox", produce=("song.wav", "song.mid"))
    result = await client.call_tool(
        "text2vox", {"reference_file": str(ref), "lyrics": "la la", "midi_file": str(mid)}
    )
    assert not result.is_error and calls[0][1]["midi_file"] == str(mid.resolve())


@pytest.mark.anyio
async def test_list_results_reports_previous_jobs(client, settings, wav, monkeypatch):
    fake_sdk(monkeypatch, "analyze", produce=("analysis.json",))
    await client.call_tool("analyze", {"file_path": str(wav)})
    result = await client.call_tool("list_results", {"tool": "analyze", "limit": 5})
    jobs = result.structured_content["result"]
    assert len(jobs) == 1 and jobs[0]["files"][0]["name"] == "analysis.json"


@pytest.mark.anyio
async def test_summary_text_mentions_folder(client, wav, monkeypatch):
    fake_sdk(monkeypatch, "master", produce=("mastered.wav",))
    result = await client.call_tool("master", {"file_path": str(wav)})
    text = result.content[0].text
    assert "master" in text and "mastered.wav" in json.dumps(result.structured_content)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_server.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'audial_mcp.server'`

- [ ] **Step 3: Implement `server.py`**

```python
"""audial MCP server: tools that wrap the Audial SDK."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import audial
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from audial_mcp import __version__
from audial_mcp.config import Settings, load_settings
from audial_mcp.errors import to_tool_error
from audial_mcp.jobs import run_job
from audial_mcp.results import JobResult, inventory, list_jobs, new_job_dir, slugify
from audial_mcp.validation import AUDIO_EXTENSIONS, JSON_EXTENSIONS, MIDI_EXTENSIONS, check_file

log = logging.getLogger("audial_mcp")

INSTRUCTIONS = (
    "Audial runs hosted audio tools. Give tools full local file paths. Results are written to "
    "the user's Audial results folder and returned as file paths; tell the user where they are. "
    "Jobs take from a few seconds (analyze) to a few minutes (stem_split, generate_music, "
    "sound2vital, text2vox); progress notifications report elapsed time. Some tools need an "
    "active Audial subscription; relay the error text if one is returned."
)

mcp = MCPServer("audial", instructions=INSTRUCTIONS, version=__version__)

_settings: Settings = load_settings()

REMOTE = ToolAnnotations(read_only_hint=False, open_world_hint=True, idempotent_hint=False)
READ_ONLY_REMOTE = ToolAnnotations(read_only_hint=True, open_world_hint=True)
LOCAL_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _execution_id(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("execution_id", "exe_id", "exeId"):
        if isinstance(raw.get(key), str):
            return raw[key]
    execution = raw.get("execution")
    if isinstance(execution, dict):
        for key in ("exeId", "exe_id", "execution_id"):
            if isinstance(execution.get(key), str):
                return execution[key]
    return None


def _metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"result": raw}
    return {k: v for k, v in raw.items() if k != "files"}


async def _execute(ctx: Context, tool: str, slug: str, call: Callable[[Path], Any]) -> JobResult:
    """Common path: credentials → job folder → run in thread → inventory → JobResult."""
    execution_id: str | None = None
    try:
        _settings.require_credentials()
        if _settings.api_base_url:
            os.environ["AUDIAL_API_BASE_URL"] = _settings.api_base_url
        os.environ["AUDIAL_USER_ID"] = _settings.user_id or ""
        os.environ["AUDIAL_API_KEY"] = _settings.api_key or ""
        output_dir = new_job_dir(_settings.results_dir, tool, slug)
        started = time.monotonic()
        await ctx.info(f"{tool}: starting; results → {output_dir}")
        raw = await run_job(ctx, tool, lambda: call(output_dir), timeout_s=_settings.job_timeout_s)
        execution_id = _execution_id(raw)
        files = inventory(output_dir)
        elapsed = int(time.monotonic() - started)
        summary = f"{tool} finished in {elapsed} s: {len(files)} file(s) in {output_dir}"
        await ctx.info(summary)
        return JobResult(
            tool=tool,
            execution_id=execution_id,
            output_dir=str(output_dir),
            files=files,
            metadata=_metadata(raw),
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes a ToolError
        raise to_tool_error(exc, tool=tool, execution_id=execution_id) from None


FilePath = Annotated[
    str,
    Field(
        description="Full path to a local audio file (.wav, .mp3, .aif, .aiff, .flac, .m4a, .ogg, .aac). ~ is expanded."
    ),
]


@mcp.tool(title="Split stems", annotations=REMOTE)
async def stem_split(
    ctx: Context,
    file_path: FilePath,
    stems: Annotated[
        list[str] | None,
        Field(
            description="Stems to extract from: vocals, drums, bass, other, full_song_without_vocals. Default: vocals, drums, bass, other."
        ),
    ] = None,
    target_bpm: Annotated[
        float | None, Field(description="Retime the stems to this tempo (beats per minute).")
    ] = None,
    target_key: Annotated[
        str | None,
        Field(description="Transpose the stems to this key, e.g. 'A minor' or 'F# major'."),
    ] = None,
    algorithm: Annotated[
        str, Field(description="Separation algorithm. Default 'primaudio'.")
    ] = "primaudio",
) -> JobResult:
    """Split a track into separate instrument stems, optionally retimed and rekeyed."""
    src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
    return await _execute(
        ctx,
        "stem_split",
        slugify(src.stem),
        lambda out: audial.stem_split(
            file_path=str(src),
            stems=stems,
            target_bpm=target_bpm,
            target_key=target_key,
            algorithm=algorithm,
            results_folder=str(out),
        ),
    )


@mcp.tool(title="Analyze audio", annotations=READ_ONLY_REMOTE)
async def analyze(ctx: Context, file_path: FilePath) -> JobResult:
    """Analyze a track: BPM, key, loudness and other characteristics. Results are in `metadata`."""
    src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
    return await _execute(
        ctx,
        "analyze",
        slugify(src.stem),
        lambda out: audial.analyze(file_path=str(src), results_folder=str(out)),
    )


@mcp.tool(title="Segment audio", annotations=REMOTE)
async def segment(
    ctx: Context,
    file_path: FilePath,
    components: Annotated[
        list[str] | None, Field(description="Components to analyze, e.g. ['vocals', 'drums'].")
    ] = None,
    analysis_type: Annotated[
        str | None, Field(description="Analysis type accepted by Audial's segmentation.")
    ] = None,
    features: Annotated[
        list[str] | None, Field(description="Features to compute per segment.")
    ] = None,
    genre: Annotated[
        str | None, Field(description="Genre hint that improves section detection.")
    ] = None,
) -> JobResult:
    """Detect song sections (intro, verse, chorus…) and analyze components within them."""
    src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
    return await _execute(
        ctx,
        "segment",
        slugify(src.stem),
        lambda out: audial.segment(
            file_path=str(src),
            components=components,
            analysis_type=analysis_type,
            features=features,
            genre=genre,
            results_folder=str(out),
        ),
    )


@mcp.tool(title="Master a track", annotations=REMOTE)
async def master(
    ctx: Context,
    file_path: FilePath,
    reference_file: Annotated[
        str | None,
        Field(
            description="Optional reference track whose loudness and tone the master should match."
        ),
    ] = None,
) -> JobResult:
    """Master a mix, optionally matching a reference track."""
    src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
    ref = (
        check_file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
        if reference_file
        else None
    )
    return await _execute(
        ctx,
        "master",
        slugify(src.stem),
        lambda out: audial.master(
            file_path=str(src), reference_file=str(ref) if ref else None, results_folder=str(out)
        ),
    )


@mcp.tool(title="Generate a sample pack", annotations=REMOTE)
async def generate_samples(
    ctx: Context,
    file_path: FilePath,
    job_type: Annotated[
        str | None,
        Field(
            description="Sample pack job type accepted by Audial (default engine choice when omitted)."
        ),
    ] = None,
    components: Annotated[
        list[str] | None, Field(description="Which components to sample, e.g. ['drums', 'bass'].")
    ] = None,
    genre: Annotated[str | None, Field(description="Genre hint.")] = None,
) -> JobResult:
    """Extract a sample pack (one-shots and loops) from a track."""
    src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
    return await _execute(
        ctx,
        "generate_samples",
        slugify(src.stem),
        lambda out: audial.generate_samples(
            file_path=str(src),
            job_type=job_type,
            components=components,
            genre=genre,
            results_folder=str(out),
        ),
    )


@mcp.tool(title="Audio to MIDI", annotations=REMOTE)
async def generate_midi(
    ctx: Context,
    file_path: FilePath,
    bpm: Annotated[
        float | None, Field(description="Override the detected tempo for note quantisation.")
    ] = None,
) -> JobResult:
    """Transcribe an audio file to MIDI."""
    src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
    return await _execute(
        ctx,
        "generate_midi",
        slugify(src.stem),
        lambda out: audial.generate_midi(
            file_path=str(src), bpm=bpm, results_folder=str(out), max_wait=_settings.job_timeout_s
        ),
    )


@mcp.tool(title="Generate music", annotations=REMOTE)
async def generate_music(
    ctx: Context,
    prompt: Annotated[
        str,
        Field(
            description="Style description: genre, mood, instruments, vocal character. Tempo and key words in the prompt steer the model more than the numeric bpm/key fields, which are hints, not constraints."
        ),
    ],
    task_type: Annotated[
        str,
        Field(
            description="text2music (default), cover, remix, extract, lego, complete, or understand."
        ),
    ] = "text2music",
    lyrics: Annotated[
        str | None,
        Field(
            description="Lyrics with optional section tags like [Verse] and [Chorus]. Omit for instrumental."
        ),
    ] = None,
    source_file: Annotated[
        str | None,
        Field(
            description="Local audio file; required for remix, extract, lego, complete, understand."
        ),
    ] = None,
    reference_file: Annotated[
        str | None, Field(description="Local audio file; required for cover.")
    ] = None,
    bpm: Annotated[int | None, Field(description="Tempo hint in beats per minute.")] = None,
    key_scale: Annotated[str | None, Field(description="Key hint, e.g. 'G major'.")] = None,
    time_signature: Annotated[str | None, Field(description="e.g. '4/4'.")] = None,
    audio_duration: Annotated[
        float | None, Field(description="Length in seconds (10–600).")
    ] = None,
    vocal_language: Annotated[
        str, Field(description="Language code for vocals, e.g. 'en'.")
    ] = "en",
    audio_cover_strength: Annotated[
        float | None, Field(description="0–1 fidelity to the original for cover/remix.")
    ] = None,
    repainting_start: Annotated[
        float | None, Field(description="Remix start time in seconds.")
    ] = None,
    repainting_end: Annotated[
        float | None, Field(description="Remix end time in seconds (-1 = end).")
    ] = None,
    batch_size: Annotated[int, Field(description="Number of variations, 1–8.")] = 1,
    seed: Annotated[int | None, Field(description="Seed for reproducible output.")] = None,
    audio_format: Annotated[str, Field(description="mp3, wav, flac, opus or aac.")] = "mp3",
    track_name: Annotated[
        str | None,
        Field(
            description="Track to extract/replace for extract/lego: vocals, drums, bass, guitar, piano, strings, synth, other."
        ),
    ] = None,
    instrumental: Annotated[bool, Field(description="Generate without vocals.")] = False,
    negative_prompt: Annotated[
        str | None, Field(description="What to avoid, comma-separated.")
    ] = None,
) -> JobResult:
    """Generate music with the Audial music model: text to music, covers, remixes, stem extraction, completion, or analysis (understand)."""
    src = check_file(source_file, AUDIO_EXTENSIONS, "source audio file") if source_file else None
    ref = (
        check_file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
        if reference_file
        else None
    )
    slug = slugify("_".join(prompt.split()[:3]), fallback="music")
    return await _execute(
        ctx,
        "generate_music",
        slug,
        lambda out: audial.generate_music(
            prompt=prompt,
            task_type=task_type,
            lyrics=lyrics,
            source_file=str(src) if src else None,
            reference_file=str(ref) if ref else None,
            bpm=bpm,
            key_scale=key_scale,
            time_signature=time_signature,
            audio_duration=audio_duration,
            vocal_language=vocal_language,
            audio_cover_strength=audio_cover_strength,
            repainting_start=repainting_start,
            repainting_end=repainting_end,
            batch_size=batch_size,
            seed=seed,
            audio_format=audio_format,
            track_name=track_name,
            instrumental=instrumental,
            negative_prompt=negative_prompt,
            results_folder=str(out),
            max_wait=_settings.job_timeout_s,
        ),
    )


@mcp.tool(title="Resynthesize a one-shot into a synth preset", annotations=REMOTE)
async def sound2vital(
    ctx: Context,
    file_path: Annotated[
        str,
        Field(
            description="A short one-shot (≤ 20 s) audio file. The result is an editable Audial Synth (.vital) preset that reproduces its timbre."
        ),
    ],
) -> JobResult:
    """Turn a one-shot sample into an editable Audial Synth preset (requires an Audial subscription)."""
    src = check_file(file_path, AUDIO_EXTENSIONS, "audio file")
    return await _execute(
        ctx,
        "sound2vital",
        slugify(src.stem),
        lambda out: audial.sound2vital(
            file_path=str(src), results_folder=str(out), max_wait=_settings.job_timeout_s
        ),
    )


@mcp.tool(title="Sing lyrics in a reference voice", annotations=REMOTE)
async def text2vox(
    ctx: Context,
    reference_file: Annotated[
        str, Field(description="Short clip of the reference voice (timbre).")
    ],
    lyrics: Annotated[str, Field(description="Lyrics to sing.")],
    midi_file: Annotated[
        str | None, Field(description="MIDI melody (.mid). Give this or melody_audio_file.")
    ] = None,
    melody_audio_file: Annotated[
        str | None, Field(description="Audio whose melody is transcribed and followed, if no MIDI.")
    ] = None,
    word_timestamps_file: Annotated[
        str | None, Field(description="Optional JSON word timings.")
    ] = None,
    lyrics_mode: Annotated[
        str, Field(description="auto (default) or an explicit lyric alignment mode.")
    ] = "auto",
    reference_text: Annotated[
        str | None, Field(description="Transcript of the reference clip, if known.")
    ] = None,
    cfg_strength: Annotated[float | None, Field(description="Guidance strength.")] = None,
    nfe_steps: Annotated[
        int | None, Field(description="Synthesis steps; more is slower and cleaner.")
    ] = None,
    pitch_shift: Annotated[
        float | None, Field(description="Semitones to shift the melody.")
    ] = None,
    strict_pitch: Annotated[
        bool | None, Field(description="Force exact pitch to the melody.")
    ] = None,
    bend_smoothing_ms: Annotated[
        float | None, Field(description="Pitch-bend smoothing window in ms.")
    ] = None,
    no_pitch_bends: Annotated[bool | None, Field(description="Disable pitch bends.")] = None,
    leading_silence_s: Annotated[
        float | None, Field(description="Silence before the first note, seconds.")
    ] = None,
    seed: Annotated[int | None, Field(description="Seed for reproducible output.")] = None,
) -> JobResult:
    """Synthesize a sung vocal (and MIDI) from lyrics, a melody and a reference voice (requires an Audial subscription)."""
    ref = check_file(reference_file, AUDIO_EXTENSIONS, "reference audio file")
    mid = check_file(midi_file, MIDI_EXTENSIONS, "MIDI file") if midi_file else None
    mel = (
        check_file(melody_audio_file, AUDIO_EXTENSIONS, "melody audio file")
        if melody_audio_file
        else None
    )
    wts = (
        check_file(word_timestamps_file, JSON_EXTENSIONS, "word timestamps JSON file")
        if word_timestamps_file
        else None
    )
    return await _execute(
        ctx,
        "text2vox",
        slugify(ref.stem),
        lambda out: audial.text2vox(
            reference_file=str(ref),
            lyrics=lyrics,
            midi_file=str(mid) if mid else None,
            melody_audio_file=str(mel) if mel else None,
            word_timestamps_file=str(wts) if wts else None,
            lyrics_mode=lyrics_mode,
            reference_text=reference_text,
            cfg_strength=cfg_strength,
            nfe_steps=nfe_steps,
            pitch_shift=pitch_shift,
            strict_pitch=strict_pitch,
            bend_smoothing_ms=bend_smoothing_ms,
            no_pitch_bends=no_pitch_bends,
            leading_silence_s=leading_silence_s,
            seed=seed,
            results_folder=str(out),
            max_wait=_settings.job_timeout_s,
        ),
    )


@mcp.tool(title="List previous results", annotations=LOCAL_READ_ONLY)
async def list_results(
    tool: Annotated[
        str | None, Field(description="Filter by tool name, e.g. 'stem_split'. Omit for all tools.")
    ] = None,
    limit: Annotated[
        int, Field(description="Maximum number of jobs to return, newest first.", ge=1, le=100)
    ] = 10,
) -> list[JobResult]:
    """List previous Audial results in the results folder, newest first."""
    return list_jobs(_settings.results_dir, tool, limit)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    global _settings
    _settings = load_settings()
    log.info(
        "audial-mcp %s; results dir %s; credentials %s",
        __version__,
        _settings.results_dir,
        "present" if _settings.user_id and _settings.api_key else "MISSING",
    )
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
```

Implementation notes for whoever runs this task:
- Parameter validation (`check_file`) runs before `_execute`, outside the try; wrap those calls so a `ValidationError` also becomes a `ToolError` — simplest is to move the `check_file` calls inside a small helper `_validated(fn)` or catch in each tool. Choose one and keep all ten tools consistent (the tests require `is_error` with the message text).
- `analyze` is annotated read-only but still uploads; the hint describes side effects on the user's data, not network use.
- Verify against the installed `mcp` version that `Context` is importable from `mcp.server.mcpserver` and that `Client(...).call_tool` returns `structured_content` for TypedDict returns; adjust imports if the 2.2.x layout differs, and record the exact version in `uv.lock`.

- [ ] **Step 4: Run all tests and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all tests pass (including the one where the fake SDK prints to stdout); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "server: MCPServer with ten Audial tools"
```

---

### Task 8: Manual verification with the Inspector and Claude Code (dev API)

**Files:**
- Create: `docs/manual-e2e-checklist.md`

- [ ] **Step 1: Write the checklist** (each line is a box to tick with date and result):

```markdown
# Manual e2e checklist — audial-mcp

Environment: dev API (`AUDIAL_API_BASE_URL=https://starfish-app-2x28e.ondigitalocean.app/api`),
smoke account (`SMOKE_USER_ID` / `SMOKE_USER_API_KEY` from genetic_vital/.env),
`AUDIAL_RESULTS_DIR=/tmp/audial-e2e`.

## Inspector
- [ ] `uv run mcp dev src/audial_mcp/server.py` opens the Inspector; ten tools listed with descriptions.
- [ ] `analyze` on a 10 s wav returns metadata with bpm/key; files inventoried.
- [ ] `stem_split` on a 30 s wav; progress notifications visible every 5 s; ≥ 2 stems in the job folder.
- [ ] `generate_music` 30 s text2music; returns mp3; `metadata.execution.generation_metadata.lm_model == "audial-music-1"`.
- [ ] `sound2vital` on a one-shot returns a .vital file.
- [ ] `text2vox` with reference + MIDI returns song.wav and song.mid.
- [ ] `list_results` shows the five jobs newest first; `tool="analyze"` filters.
- [ ] Missing file → readable error; unset AUDIAL_API_KEY → readable error; unsubscribed account → 402 text relayed.

## Claude Code
- [ ] `claude mcp add audial-dev -e … -- uv run --directory <repo> audial-mcp` then `/mcp` shows connected.
- [ ] "Split ~/x.wav into vocals and drums" → tool call approved → paths reported → files exist.
- [ ] Kill the client mid-job: server process exits with the client (no orphaned `audial-mcp`).

## Prod (after Task 9 publishes 0.1.0)
- [ ] `claude mcp add audial -e … -- uvx audial-mcp` with the zfmoodydub account: stem_split + generate_music succeed against api.audialmusic.ai.
- [ ] Windows: not tested (see design open question 6).
```

- [ ] **Step 2: Run the Inspector section** against dev; fix anything found (each fix is its own commit with a test where possible).
- [ ] **Step 3: Run the Claude Code section.**
- [ ] **Step 4: Commit the ticked checklist**

```bash
git add -A && git commit -m "docs: manual e2e checklist with dev results"
```

---

### Task 9: CI, GitHub repository and PyPI release 0.1.0

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `docs/release-checklist.md`

- [ ] **Step 1: `ci.yml`**

```yaml
name: ci
on:
  push: { branches: [main] }
  pull_request:
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.10", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "${{ matrix.python }}" }
      - run: uv sync --group dev
      - run: uv run ruff check . && uv run ruff format --check .
      - run: uv run pytest -q
```

- [ ] **Step 2: `publish.yml`** (uses the existing PyPI token as secret `PYPI_TOKEN`; switch to trusted publishing later if Zach prefers — open question 7)

```yaml
name: publish
on:
  push:
    tags: ["v*"]
jobs:
  pypi:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --group dev && uv run pytest -q
      - run: uv build
      - run: uv publish --token "${{ secrets.PYPI_TOKEN }}"
```

- [ ] **Step 3: `docs/release-checklist.md`**

```markdown
# Release checklist
1. Bump `version` in pyproject.toml and `__version__`; update `server.json` version; update the
   Claude plugin's `.mcp.json` pin (`uvx audial-mcp==X.Y.Z`) and plugin.json version.
2. `uv run pytest -q`, ruff clean, manual checklist "Prod" section on the previous version.
3. `git tag -a vX.Y.Z -m "audial-mcp X.Y.Z" && git push origin main vX.Y.Z` → publish workflow.
4. Verify `pip index versions audial-mcp` / PyPI page; `uvx audial-mcp==X.Y.Z --help` starts.
5. `mcp-publisher publish` (Task 10) so the registry shows the new version.
6. Push the plugin repo; the Claude directory picks up the tracked branch automatically.
7. Update the website MCP tab if tools or config changed.
```

- [ ] **Step 4: Create the GitHub repository and push (APPROVAL: outward-facing; Zach confirms the repo name `AudialAI/audial-mcp`, public, MIT)**

```bash
gh repo create AudialAI/audial-mcp --public --source . --remote origin --description "MCP server for Audial's hosted audio tools" --push
gh secret set PYPI_TOKEN --repo AudialAI/audial-mcp --body "$PYPI_TOKEN"
```

Wait for `ci` to pass on `main`.

- [ ] **Step 5: Tag and publish 0.1.0 (APPROVAL: publishing to PyPI)**

```bash
git tag -a v0.1.0 -m "audial-mcp 0.1.0" && git push origin v0.1.0
```

Verify: PyPI shows 0.1.0; `uvx audial-mcp==0.1.0` starts and logs "audial-mcp 0.1.0" to stderr; run the "Prod" section of the manual checklist.

---

### Task 10: Official MCP registry listing

**Files:**
- Create: `server.json`

- [ ] **Step 1: Write `server.json`** (namespace per open question 1; default `io.github.AudialAI`)

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.AudialAI/audial-mcp",
  "description": "Stem splitting, analysis, mastering, sample packs, audio-to-MIDI, music generation, resynthesis and vocal synthesis with Audial's hosted engines.",
  "version": "0.1.0",
  "repository": { "url": "https://github.com/AudialAI/audial-mcp", "source": "github" },
  "websiteUrl": "https://audialmusic.ai/resources/api-reference",
  "packages": [
    {
      "registryType": "pypi",
      "registryBaseUrl": "https://pypi.org",
      "identifier": "audial-mcp",
      "version": "0.1.0",
      "runtimeHint": "uvx",
      "transport": { "type": "stdio" },
      "environmentVariables": [
        { "name": "AUDIAL_USER_ID", "description": "Audial user id", "isRequired": true, "isSecret": false },
        { "name": "AUDIAL_API_KEY", "description": "Audial API key", "isRequired": true, "isSecret": true },
        { "name": "AUDIAL_RESULTS_DIR", "description": "Folder results are written to", "default": "~/Audial" }
      ]
    }
  ]
}
```

Check the exact `$schema` URL against the registry's `docs/reference/server-json` at execution time and use the current one.

- [ ] **Step 2: Confirm the README on PyPI contains `mcp-name: io.github.AudialAI/audial-mcp`** (it does from Task 1; the registry validates this against the published package).

- [ ] **Step 3: Publish (Zach runs the login; it opens a browser)**

```bash
brew install mcp-publisher
cd audial-mcp
mcp-publisher login github      # Zach: authenticate as a member of the AudialAI org
mcp-publisher publish
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.AudialAI/audial-mcp"
```

- [ ] **Step 4: Commit `server.json`**

```bash
git add server.json && git commit -m "registry: server.json for the official MCP registry" && git push
```

---

### Task 11: Claude plugin bundle and directory submission

**Files (new repo `AudialAI/audial-claude-plugin`, plugin at the repo root):**
- Create: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.mcp.json`, `skills/audial/SKILL.md`, `README.md`, `LICENSE`

- [ ] **Step 1: `plugin.json`**

```json
{
  "name": "audial",
  "displayName": "Audial",
  "version": "0.1.0",
  "description": "Stem splitting, mastering, audio-to-MIDI, music generation, resynthesis and vocal synthesis with Audial's hosted engines.",
  "author": { "name": "Audial", "email": "contact@audialmusic.ai", "url": "https://audialmusic.ai" },
  "homepage": "https://audialmusic.ai/resources/api-reference",
  "repository": "https://github.com/AudialAI/audial-claude-plugin",
  "license": "MIT",
  "keywords": ["audio", "music", "stems", "mastering", "midi"],
  "userConfig": {
    "user_id": { "type": "string", "title": "Audial user id", "description": "From your dashboard at audialmusic.ai", "required": true },
    "api_key": { "type": "string", "title": "Audial API key", "description": "From your dashboard at audialmusic.ai", "sensitive": true, "required": true },
    "results_dir": { "type": "directory", "title": "Results folder", "description": "Where Audial writes stems, masters, MIDI and generated audio", "default": "~/Audial" }
  }
}
```

- [ ] **Step 2: `.mcp.json`** (pinned version is mandatory for the directory validator)

```json
{
  "mcpServers": {
    "audial": {
      "command": "uvx",
      "args": ["audial-mcp==0.1.0"],
      "env": {
        "AUDIAL_USER_ID": "${user_config.user_id}",
        "AUDIAL_API_KEY": "${user_config.api_key}",
        "AUDIAL_RESULTS_DIR": "${user_config.results_dir}"
      }
    }
  }
}
```

- [ ] **Step 3: `marketplace.json`** (lets users install before the directory approves)

```json
{
  "name": "audial",
  "owner": { "name": "Audial" },
  "plugins": [
    { "name": "audial", "source": "./", "description": "Audial audio tools as MCP tools" }
  ]
}
```

- [ ] **Step 4: `skills/audial/SKILL.md`**

```markdown
---
name: audial
description: Use when the user wants to split stems, analyze or master audio, make a sample pack, convert audio to MIDI, generate music, turn a one-shot into a synth preset, or sing lyrics in a reference voice. Routes each request to the right Audial MCP tool and explains results.
---

# Audial tools

All tools take full local file paths and write results into the user's Audial results folder.
Report the `output_dir` and file names back to the user after every job.

| User wants | Tool | Notes |
|---|---|---|
| separate vocals/drums/bass, "remove vocals", change tempo/key of stems | `stem_split` | `full_song_without_vocals` gives an instrumental |
| tempo, key, loudness of a track | `analyze` | fast; answer from `metadata` |
| song sections, arrangement | `segment` | |
| louder/polished mix, "master this" | `master` | pass a reference track if they name one |
| drum hits, loops, sample pack | `generate_samples` | |
| audio → MIDI | `generate_midi` | |
| write/generate a song, cover, remix, extend | `generate_music` | tempo/key words in the prompt beat the numeric fields |
| synth patch from a sample | `sound2vital` | one-shot ≤ 20 s; needs subscription |
| sing these lyrics in this voice | `text2vox` | needs reference clip and a melody (MIDI or audio); needs subscription |
| "where did that go", earlier outputs | `list_results` | |

Jobs run remotely and take seconds to minutes; progress is reported. If a tool returns a
subscription or credentials message, relay it verbatim and stop.
```

- [ ] **Step 5: README (≥ 40 words, states what leaves the machine) and MIT LICENSE**, then validate:

```bash
claude plugin validate .
```
Expected: `✔ Validation passed`.

- [ ] **Step 6: Create the repo and push (APPROVAL: outward-facing)**

```bash
gh repo create AudialAI/audial-claude-plugin --public --source . --remote origin --push
```

Local install test: `claude plugin marketplace add AudialAI/audial-claude-plugin && claude plugin install audial@audial`, answer the userConfig prompts, run a stem split.

- [ ] **Step 7: Directory submission (Zach, needs a paid claude.ai plan):** claude.ai/directory/manage → Submit new → Plugin bundle → repository `AudialAI/audial-claude-plugin`, folder `/` → Validate → fix Blocking findings (expect a reviewer hold "Runs a pinned uvx package", which is normal) → Submit for review.

---

### Task 12: Website — MCP tab on the API reference page and Cursor button

**Files:**
- Modify: `audial-fe-re/audial-fe-re/src/pages/resources/ApiReference.tsx` (`tabs` array at line 84; new `activeTab === "mcp"` block after the SDKs block; Overview "3. Navigate to the SDK tab" paragraph gets a sentence pointing at the MCP tab)
- Create: `audial-fe-re/audial-fe-re/src/pages/resources/mcpTab.tsx` (content component, keeps ApiReference.tsx from growing further)
- Create: `audial-fe-re/audial-fe-re/src/utils/cursorInstallLink.ts`
- Test: `audial-fe-re/audial-fe-re/src/utils/cursorInstallLink.test.ts` (only if the repo has a test runner configured; otherwise verify by `npm run build` and a headless screenshot as done for the tab bar)

- [ ] **Step 1: `cursorInstallLink.ts`**

```ts
// Builds the "Add to Cursor" deeplink: cursor://anysphere.cursor-deeplink/mcp/install?name=…&config=<base64 JSON>
export interface StdioServerConfig {
	command: string;
	args: string[];
	env?: Record<string, string>;
}

export function cursorInstallLink(name: string, config: StdioServerConfig): string {
	const json = JSON.stringify(config);
	const base64 = typeof window === "undefined" ? Buffer.from(json).toString("base64") : window.btoa(unescape(encodeURIComponent(json)));
	return `cursor://anysphere.cursor-deeplink/mcp/install?name=${encodeURIComponent(name)}&config=${encodeURIComponent(base64)}`;
}

export const AUDIAL_MCP_CONFIG: StdioServerConfig = {
	command: "uvx",
	args: ["audial-mcp"],
	env: { AUDIAL_USER_ID: "YOUR_USER_ID", AUDIAL_API_KEY: "YOUR_API_KEY", AUDIAL_RESULTS_DIR: "~/Audial" },
};
```

- [ ] **Step 2: `mcpTab.tsx`** — sections, in this order, using the page's existing classes (`glass-card rounded-xl p-6`, `<pre className="text-cyan-500 whitespace-pre-wrap">`, headings `text-2xl font-bold dark:text-white text-slate-100`):
  1. **What it is** — two sentences from the design §1; the results-folder rule.
  2. **Requirements** — Audial account (user id + API key from the dashboard), `uv` install line for macOS/Linux and Windows.
  3. **Install** — sub-blocks: Claude Code (`claude mcp add …` line), Claude Desktop (`claude_desktop_config.json` JSON with the path on macOS/Windows), Cursor (`<a href={cursorInstallLink("audial", AUDIAL_MCP_CONFIG)}>Add to Cursor</a>` button plus the same JSON for manual `~/.cursor/mcp.json`), Other clients (the JSON).
  4. **Tools** — table with the ten tools, one-line purpose, "needs subscription" column for sound2vital/text2vox.
  5. **Results** — `~/Audial/<tool>/<timestamp>_<name>/`, `list_results`.
  6. **Troubleshooting** — "server failed to start" (uv missing), credentials error text, 402 text, timeouts (`AUDIAL_JOB_TIMEOUT_S`), where logs go (client's MCP log; server writes to stderr).
  7. **Links** — GitHub, PyPI, MCP registry entry, Claude plugin.

- [ ] **Step 3: Wire the tab**

In `ApiReference.tsx`: add `{ id: "mcp", label: "MCP" }` after `sdks` in `tabs`; render `{activeTab === "mcp" && <McpTab />}` after the SDKs block; in the Overview tab's "3. Navigate to the SDK tab" section add: "Using Claude Code, Claude Desktop or Cursor? The MCP tab installs Audial as tools in your assistant."

- [ ] **Step 4: Verify**

Run: `npm run build && npm run lint`; serve `vite preview` and capture `/resources/api-reference` headless at 1280 and 500 px with the MCP tab active (add `?tab=mcp` support only if trivial; otherwise screenshot after clicking is not possible headlessly, so temporarily default `activeTab` to `"mcp"` for the screenshot and revert).
Expected: build and lint green; tab renders with all seven sections; the Cursor link href starts with `cursor://anysphere.cursor-deeplink/mcp/install?name=audial&config=`.

- [ ] **Step 5: PR and merge (APPROVAL: deploys to www.audialmusic.ai)**

```bash
git checkout -b mcp-docs && git add -A && git commit -m "api reference: MCP tab with install blocks, tools and Cursor button"
git push -u origin mcp-docs && gh pr create --repo AudialAI/audial-fe-re --base master --title "API reference: MCP tab" --body "..."
gh pr merge <n> --merge
```

Verify on production: bundle contains "Add to Cursor" and "audial-mcp".

---

### Task 13: Optional listings and wrap-up

- [ ] **Step 1: Community catalogs (Zach decides; each is a form or PR):** Smithery (`smithery.yaml` + submit), Glama (auto-indexes GitHub repos with an MCP badge; add the badge to README), PulseMCP / mcp.so (submission forms), Cursor directory (cursor.com/mcp submission; open question 3).
- [ ] **Step 2: Update `audial-sdk` README and the website SDK tab** with one line pointing at the MCP tab.
- [ ] **Step 3: Record in memory and the release plan:** repo URLs, registry name, plugin repo, "how to release" pointer; and the rule that plugin `.mcp.json` pins must be bumped on every release.
- [ ] **Step 4: Final commit and tag check:** `git status` clean in all three repos; `v0.1.0` on audial-mcp; plugin `0.1.0`; website merged.

---

## Self-review

- **Spec coverage:** §3 UX → Tasks 1, 9, 11, 12; §4 architecture → Tasks 2–7; §5 config → Task 2; §6 tools → Task 7; §7 results → Task 3; §8 long jobs → Tasks 0, 5; §9 errors → Task 6; §10 security → Task 1 README + Task 11 README; §11 distribution → Tasks 9–13; §12 testing → every task + Task 8; §13 versioning → Task 9 release checklist.
- **Placeholder scan:** none; every deployment step names the exact command and the approval gate.
- **Type consistency:** `JobResult`/`FileInfo` (Task 3) are what Task 7 returns and Task 7's tests read; `run_job` signature (Task 5) matches its call in `_execute`; `to_tool_error(exc, tool=, execution_id=)` (Task 6) matches the call in `_execute`; `check_file(path, kinds, label)` (Task 4) matches all ten tools; SDK `max_wait` kwargs used in Task 7 exist after Task 0 (`generate_music`, `generate_midi`) or already exist (`sound2vital`, `text2vox`).
