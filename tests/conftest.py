from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from make_fixtures import make  # noqa: E402

VALIDATOR = ROOT / "tools" / "tldr-validate" / "validate.mjs"


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return make(tmp_path_factory.mktemp("fixtures"))


def validate_tldr(path: Path) -> None:
    """Validate against the pinned tldraw schema. Required in CI (C2A_REQUIRE_NODE=1)."""
    node = shutil.which("node")
    have = node and (VALIDATOR.parent / "node_modules" / "@tldraw" / "tlschema").exists()
    if not have:
        if os.environ.get("C2A_REQUIRE_NODE") == "1":
            raise AssertionError("tldr validator not installed (npm ci in tools/tldr-validate)")
        pytest.skip("tldr validator not installed")
    proc = subprocess.run([node, str(VALIDATOR), str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
