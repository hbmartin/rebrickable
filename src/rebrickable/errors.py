"""Structured exception hierarchy for :mod:`rebrickable`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class RebrickableError(Exception):
    """Base class for all documented library failures."""


class ConfigLoadError(RebrickableError):
    """A present configuration file is unreadable or invalid."""


class CatalogUnavailableError(RebrickableError):
    """No ready local catalog is available."""


class CatalogUnreadableError(CatalogUnavailableError):
    """The active local catalog cannot be opened."""


class CatalogSchemaError(CatalogUnavailableError):
    """The active catalog schema is incompatible with this release."""


class EntityNotFoundError(RebrickableError):
    """A required entity does not exist in the active snapshot."""

    def __init__(self, kind: str, identifier: str | int) -> None:
        self.kind = kind
        self.identifier = identifier
        super().__init__(f"{kind} not found: {identifier}")


class DownloadError(RebrickableError):
    """A catalog dataset could not be downloaded."""


class DatasetIntegrityError(RebrickableError):
    """A dataset or cross-dataset relationship failed validation."""


class DatasetSchemaError(RebrickableError):
    """A downloaded CSV is missing required columns or has invalid values."""


class CatalogImportError(RebrickableError):
    """A validated catalog could not be imported or promoted."""


@dataclass(frozen=True, slots=True)
class ApiError(RebrickableError):
    """Safe, structured details about a failed API operation."""

    status: int | None
    path_template: str
    operation_id: str
    detail: str
    request_id: str | None = None
    retry_after: float | None = None

    def __str__(self) -> str:
        status = "transport" if self.status is None else str(self.status)
        return f"{self.operation_id} failed ({status}): {self.detail}"


class ApiAuthenticationError(ApiError):
    """The API key was rejected."""


class ApiForbiddenError(ApiError):
    """The caller may not perform the operation."""


class ApiNotFoundError(ApiError):
    """The requested API entity was not found."""


class ApiThrottledError(ApiError):
    """The request remained throttled after conservative retries."""


class ApiServerError(ApiError):
    """The upstream service failed after allowed retries."""


class ApiDecodeError(ApiError):
    """A successful response did not match its required contract."""

    def __init__(
        self,
        *,
        path_template: str,
        operation_id: str,
        detail: str,
        payload: Any | None = None,
    ) -> None:
        del payload  # Never retain or repr a potentially sensitive response.
        super().__init__(None, path_template, operation_id, detail)


class PaginationCycleError(RebrickableError):
    """An API pager returned a URL already visited."""


class UnsafePaginationUrlError(RebrickableError):
    """An API pager attempted to leave the configured HTTPS origin."""


class InventoryNotFoundError(EntityNotFoundError):
    """No newest-version inventory exists for an owner."""


class InventoryCycleError(RebrickableError):
    """Recursive BOM expansion encountered an owner cycle."""

    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path
        super().__init__("inventory cycle: " + " -> ".join(path))


class UserTokenRequiredError(RebrickableError):
    """A user operation has no call-level or client-level user token."""


class OptionalDependencyError(RebrickableError):
    """An optional integration dependency is not installed."""
