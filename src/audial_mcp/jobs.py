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

    # Exceptions (other than cancellation) are caught here rather than left to
    # propagate out of the `async with` block: anyio's TaskGroup.__aexit__
    # unconditionally wraps any non-CancelledError exception raised in the
    # block's body in a BaseExceptionGroup, even when there is only one.
    # Catching and re-raising after the block exits keeps the caller-facing
    # exception a bare JobTimeout / the original error. CancelledError is left
    # to propagate normally; anyio already excludes it from that wrapping.
    error: Exception | None = None
    result: T | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat)
        try:
            with anyio.fail_after(timeout_s):
                # Cancelling to_thread does not stop the thread; it finishes quietly.
                result = await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)
        except TimeoutError:
            error = JobTimeout(label, time.monotonic() - started)
        except Exception as exc:
            error = exc
        finally:
            tg.cancel_scope.cancel()
    if error is not None:
        raise error
    return result
