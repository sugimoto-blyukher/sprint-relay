import pytest


@pytest.fixture
def inline_worker():
    """Run Discord's synchronous service callbacks in the test process."""
    async def run(call, *args, **kwargs):
        return call(*args, **kwargs)
    return run
