from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from cratebot.db import Database


@pytest_asyncio.fixture
async def db() -> Database:
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()
