"""Forward what a running Audial job reports to the MCP client.

Two kinds of news arrive while a job runs, and both reach the client the same
way:

* **Warm-up.** Audial's GPU workers scale to zero, so the first job after an
  idle period waits a few minutes for one to start. The API writes that phase
  onto the execution (and onto each pipeline step) as `warmup.state`, and the
  SDK's `audial.warmup` turns it into the one sentence every Audial client
  shows -- website, CLI, plugins and this server -- so a cold start is
  explained instead of looking like a hang.
* **Pipeline steps.** `segment` and `generate_samples` run as pipelines: stem
  separation first, then the step the user asked for. The SDK reports each
  poll and `audial.pipelines.StepTracker` reduces it to what changed.

The SDK reports both to an `on_progress` callback, but it runs the job in a
worker thread (see `jobs.run_job`), and the MCP context's `report_progress` /
`info` are coroutines belonging to the event loop.

`ProgressRelay` bridges the two: the callback only puts changes on a
thread-safe queue, and `drain` -- awaited from the event loop while the job
runs -- turns them into progress notifications and log lines.
"""

from __future__ import annotations

import contextlib
import queue
from typing import Any

from audial import pipelines as sdk_pipelines
from audial import warmup as sdk_warmup

# Step states that mean a step will not run again, for the progress fraction.
_DONE_STATES = frozenset({"completed", "skipped"})


class ProgressRelay:
    """Job news on its way from the SDK's thread to the client."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[tuple[int, int | None, str]] = queue.SimpleQueue()
        self._steps = sdk_pipelines.StepTracker()
        self._warmup = sdk_warmup.WarmupTracker()
        self.lines: list[str] = []

    def on_progress(self, status: Any) -> None:
        """SDK progress callback. Runs in the worker thread: enqueue only.

        The SDK hands over the whole status on every poll; what the client
        wants is what changed. A warm-up phase becomes its own line, because
        it is a sentence for the user to read ("Warming up the machine --
        ..."), and step changes are reduced to lines like "stem-split
        completed in 41 s", several in one poll reported together
        ("...; sample-pack running").

        A plain execution (every tool but `segment` and `generate_samples`)
        has no steps, so only the warm-up ever has anything to say.
        """
        if not isinstance(status, dict):
            return

        steps = [s for s in (status.get("steps") or []) if isinstance(s, dict)]
        done = sum(1 for s in steps if s.get("state") in _DONE_STATES)
        total = len(steps) or None

        for line in self._warmup.changes(status):
            self._queue.put((done, total, line))

        if not steps:
            # A plain execution, not a pipeline. StepTracker reads `state`
            # as a pipeline's state ("completed" means both things), so it
            # would announce "pipeline completed" for an ordinary job.
            return

        changes = self._steps.changes(status)
        if changes:
            self._queue.put((done, total, "; ".join(changes)))

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
