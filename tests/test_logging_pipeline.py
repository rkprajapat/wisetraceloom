import asyncio

import pytest
import structlog

from trailwise.logging import bind_context, configure, get_logger

# capture_logs() replaces the configured processor chain wholesale, so the
# contextvars-merging processor must be re-supplied for captured entries to
# include bound context (tenant_id, etc.) — this is what's under test here.
_CAPTURE_PROCESSORS = (structlog.contextvars.merge_contextvars,)


@pytest.fixture(autouse=True)
def _configured():
    configure()


def test_sync_logging_runs_through_pipeline():
    logger = get_logger("test.sync")
    with structlog.testing.capture_logs(processors=_CAPTURE_PROCESSORS) as captured:
        with bind_context(tenant_id="acme"):
            logger.info("hello", extra="x")

    assert len(captured) == 1
    assert captured[0]["event"] == "hello"
    assert captured[0]["extra"] == "x"
    assert captured[0]["tenant_id"] == "acme"


@pytest.mark.asyncio
async def test_async_logging_runs_through_pipeline():
    logger = get_logger("test.async")
    with structlog.testing.capture_logs(processors=_CAPTURE_PROCESSORS) as captured:
        with bind_context(tenant_id="acme-async"):
            await logger.ainfo("hello-async")

    assert len(captured) == 1
    assert captured[0]["event"] == "hello-async"
    assert captured[0]["tenant_id"] == "acme-async"


@pytest.mark.asyncio
async def test_context_isolated_across_concurrent_asyncio_tasks():
    logger = get_logger("test.isolation")

    async def worker(tenant_id: str, delay: float) -> None:
        with bind_context(tenant_id=tenant_id):
            await asyncio.sleep(delay)
            logger.info("event", worker=tenant_id)

    with structlog.testing.capture_logs(processors=_CAPTURE_PROCESSORS) as captured:
        # tenant-b finishes first (no delay); tenant-a finishes after sleeping,
        # so interleaving would leak tenant-b's context into tenant-a's log
        # if context were shared rather than task-local.
        await asyncio.gather(worker("tenant-a", 0.02), worker("tenant-b", 0.0))

    by_worker = {entry["worker"]: entry["tenant_id"] for entry in captured}
    assert by_worker == {"tenant-a": "tenant-a", "tenant-b": "tenant-b"}
