from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from make_fixtures import make  # noqa: E402


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return make(tmp_path_factory.mktemp("fixtures"))
