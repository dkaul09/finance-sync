import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def accounts_payload() -> dict:
    return load_fixture("simplefin_accounts.json")


@pytest.fixture
def settled_payload() -> dict:
    return load_fixture("simplefin_pending_settled.json")
