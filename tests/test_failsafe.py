import pytest
import structlog

from wisetraceloom.failsafe import fail_open, fail_open_context
from wisetraceloom.logging import configure


@pytest.fixture(autouse=True)
def _configured():
    configure()


def test_fail_open_decorator_passes_through_return_value_on_success():
    @fail_open()
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_fail_open_decorator_swallows_sync_exception_and_returns_none():
    @fail_open()
    def boom():
        raise RuntimeError("span export failed")

    assert boom() is None


@pytest.mark.asyncio
async def test_fail_open_decorator_swallows_async_exception_and_returns_none():
    @fail_open()
    async def boom_async():
        raise RuntimeError("async span export failed")

    assert await boom_async() is None


@pytest.mark.asyncio
async def test_fail_open_decorator_passes_through_async_return_value_on_success():
    @fail_open()
    async def add_async(a, b):
        return a + b

    assert await add_async(2, 3) == 5


def test_fail_open_context_swallows_exception_in_block():
    with fail_open_context("test-op"):
        raise RuntimeError("boom")
    # Reaching here means the exception never propagated.


def test_fail_open_does_not_swallow_keyboard_interrupt():
    @fail_open()
    def interrupt():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        interrupt()


def test_host_business_logic_exception_still_propagates_when_not_wrapped():
    # fail_open must only ever be applied around wisetraceloom's own
    # instrumentation code, never around the host's business logic — this
    # test documents that an unwrapped call still raises normally.
    def host_logic():
        raise ValueError("host bug, not an instrumentation failure")

    with pytest.raises(ValueError):
        host_logic()


def test_fail_open_logs_a_warning_event_when_swallowing():
    _CAPTURE_PROCESSORS = (structlog.contextvars.merge_contextvars,)

    @fail_open(operation="export_llm_span")
    def boom():
        raise RuntimeError("exporter down")

    with structlog.testing.capture_logs(processors=_CAPTURE_PROCESSORS) as captured:
        boom()

    assert len(captured) == 1
    assert captured[0]["event"] == "wisetraceloom_instrumentation_error"
    assert captured[0]["operation"] == "export_llm_span"
    assert captured[0]["exc_type"] == "RuntimeError"
