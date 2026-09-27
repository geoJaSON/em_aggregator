from datetime import datetime, timezone

import pytest

from emagg.demo import load_fixture

NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def fixture():
    return lambda name: load_fixture(name, NOW)
