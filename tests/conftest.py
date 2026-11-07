import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """Isolated DB per test — tests never touch the real newsroom.db."""
    monkeypatch.setenv("NEWSROOM_DB", str(tmp_path / "test.db"))
    from newsroom.storage.database import connect, migrate
    c = connect()
    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def settings():
    from newsroom.config import load_settings
    return load_settings()


@pytest.fixture()
def usgs():
    from newsroom.ingestion.adapters.usgs import UsgsAdapter
    return UsgsAdapter(name="usgs", config={})


FIXTURES = ROOT / "src" / "newsroom" / "sim" / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text().strip()
