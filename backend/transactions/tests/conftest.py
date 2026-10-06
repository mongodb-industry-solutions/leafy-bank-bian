import pytest

from shared import business_clock


@pytest.fixture(autouse=True)
def _clear_business_clock_cache():
    """The clock caches a run's offset for 5s, keyed by run id. Clear it so no test reads
    another test's run."""
    business_clock._clear_cache()
    yield
    business_clock._clear_cache()
