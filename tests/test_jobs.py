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


@pytest.mark.anyio
async def test_timeout_error_from_work_is_not_relabeled_as_job_timeout():
    ctx = FakeCtx()

    def bad():
        raise TimeoutError("socket read timed out")

    with pytest.raises(TimeoutError, match="socket read timed out"):
        await run_job(ctx, "analyze", bad, timeout_s=5, heartbeat_s=0.05)


@pytest.mark.anyio
async def test_report_progress_failure_does_not_prevent_result():
    class DisconnectedCtx(FakeCtx):
        async def report_progress(self, progress, total=None, message=None):
            raise ConnectionResetError("client disconnected")

    ctx = DisconnectedCtx()

    def work():
        time.sleep(0.2)
        return "done"

    result = await run_job(ctx, "stem_split", work, timeout_s=5, heartbeat_s=0.05)
    assert result == "done"
