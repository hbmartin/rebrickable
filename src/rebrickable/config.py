"""Non-interactive configuration with explicit secret persistence."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from rebrickable.dirs import get_cache_dir, get_config_dir, get_data_dir
from rebrickable.errors import ConfigLoadError

CONFIG_FILE = get_config_dir() / "config.yml"


@dataclass(frozen=True, slots=True)
class Config:
    """Filesystem and transport settings.

    ``api_key`` may be read from YAML or ``REBRICKABLE_API_KEY``. It is excluded
    from reprs and normal writes. API clients still require it as an explicit
    constructor argument.
    """

    database_path: Path = field(
        default_factory=lambda: get_data_dir() / "catalog.sqlite"
    )
    cache_path: Path = field(default_factory=get_cache_dir)
    base_url: str = "https://rebrickable.com/api/v3"
    downloads_base_url: str = "https://cdn.rebrickable.com/media/downloads"
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    write_timeout: float = 30.0
    pool_timeout: float = 10.0
    request_interval: float = 1.0
    max_retries: int = 3
    refresh_concurrency: int = 4
    mapping_overrides_path: Path | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)

    _PATH_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"database_path", "cache_path", "mapping_overrides_path"},
    )

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")
        if not self.downloads_base_url.startswith("https://"):
            raise ValueError("downloads_base_url must use HTTPS")
        if self.request_interval < 0:
            raise ValueError("request_interval must be non-negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.refresh_concurrency < 1:
            raise ValueError("refresh_concurrency must be positive")
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise ValueError("api_key must be a string")

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        target = CONFIG_FILE if path is None else Path(path)
        payload: dict[str, Any] = {}
        if target.exists():
            try:
                raw = yaml.safe_load(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise ConfigLoadError(f"unable to load {target}: {exc}") from exc
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                raise ConfigLoadError(f"configuration must be a mapping: {target}")
            payload = dict(raw)

        allowed = {
            item.name
            for item in cls.__dataclass_fields__.values()
            if not item.name.startswith("_")
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ConfigLoadError(
                "unknown configuration fields: " + ", ".join(sorted(unknown))
            )
        for name in cls._PATH_FIELDS:
            if name in payload and payload[name] is not None:
                if not isinstance(payload[name], str):
                    raise ConfigLoadError(f"{name} must be a path string")
                payload[name] = Path(payload[name]).expanduser()
        env_key = os.environ.get("REBRICKABLE_API_KEY")
        if env_key:
            payload["api_key"] = env_key
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise ConfigLoadError(f"invalid configuration: {exc}") from exc

    def write(
        self,
        path: Path | None = None,
        *,
        include_secrets: bool = False,
    ) -> Path:
        target = CONFIG_FILE if path is None else Path(path)
        data = asdict(self)
        if not include_secrets:
            data.pop("api_key", None)
        data = {
            key: str(value) if key in self._PATH_FIELDS and value is not None else value
            for key, value in data.items()
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            file_descriptor, temp_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                text=True,
            )
            temporary = Path(temp_name)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
            target.chmod(0o600)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ConfigLoadError(f"unable to write {target}: {exc}") from exc
        return target
