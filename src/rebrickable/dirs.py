"""Platform-standard filesystem locations."""

from __future__ import annotations

from pathlib import Path

import platformdirs

APP_NAME = "rebrickable"


def get_config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def get_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def get_cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))
