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
        rel_path = path.relative_to(folder)
        if not path.is_file() or any(part.startswith(".") for part in rel_path.parts):
            continue
        files.append(
            FileInfo(
                name=rel_path.as_posix(),
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
