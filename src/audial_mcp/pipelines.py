"""Forward pipeline step changes to the MCP client.

`segment` and `generate_samples` run as Audial pipelines: stem separation
first, then the step the user asked for. The SDK reports each poll to an
`on_progress` callback, but it runs the job in a worker thread (see
`jobs.run_job`), and the MCP context's `report_progress` / `info` are
coroutines belonging to the event loop.

`StepRelay` bridges the two: the callback only puts step changes on a
thread-safe queue, and `drain` — awaited from the event loop while the job
runs — turns them into progress notifications and log lines.
"""

from __future__ import annotations

import contextlib
import queue
from typing import Any

from audial import pipelines as sdk_pipelines

# Step states that mean a step will not run again, for the progress fraction.
_DONE_STATES = frozenset({"completed", "skipped"})


class StepRelay:
    """Pipeline step changes on their way from the SDK's thread to the client."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[tuple[int, int, str]] = queue.SimpleQueue()
        self._tracker = sdk_pipelines.StepTracker()
        self.lines: list[str] = []

    def on_progress(self, status: Any) -> None:
        """SDK progress callback. Runs in the worker thread: enqueue only.

        The SDK hands over the whole pipeline status on every poll; what the
        client wants is what changed, so `StepTracker` reduces it to lines like
        "stem-split completed in 41 s" and several changes seen in one poll are
        reported together ("...; sample-pack running").
        """
        if not isinstance(status, dict):
            return
        changes = self._tracker.changes(status)
        if not changes:
            return
        steps = [s for s in (status.get("steps") or []) if isinstance(s, dict)]
        done = sum(1 for s in steps if s.get("state") in _DONE_STATES)
        self._queue.put((done, len(steps), "; ".join(changes)))

    async def drain(self, ctx: Any) -> None:
        """Report everything queued so far. Awaited from the event loop."""
        while True:
            try:
                done, total, message = self._queue.get_nowait()
            except queue.Empty:
                return
            self.lines.append(message)
            # Reporting is best-effort in both directions: a client that has
            # gone away, or one whose context has no progress/log channel,
            # must not decide the job's outcome.
            report_progress = getattr(ctx, "report_progress", None)
            if report_progress is not None:
                with contextlib.suppress(Exception):
                    await report_progress(done, total=total, message=message)
            info = getattr(ctx, "info", None)
            if info is not None:
                with contextlib.suppress(Exception):
                    await info(message)
