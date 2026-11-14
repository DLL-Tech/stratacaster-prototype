import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stratacaster.environment import Environment  # noqa: E402
from stratacaster.llm import MockLLM  # noqa: E402


@pytest.fixture
def env(tmp_path):
    e = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="guide")
    yield e
    e.close()
