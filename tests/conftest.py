from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import settings

from rebrickable.config import Config
from tests.catalog_fixture import ROWS, build_catalog_config, write_dataset

__all__ = ["ROWS", "build_catalog_config", "write_dataset"]

settings.register_profile(
    "ci", max_examples=100, derandomize=True, deadline=None, database=None
)
settings.register_profile(
    "extended", max_examples=1000, derandomize=True, deadline=None, database=None
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture
def catalog_config(tmp_path: Path) -> Config:
    return build_catalog_config(tmp_path)
