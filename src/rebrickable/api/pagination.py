"""Pagination URL validation helpers."""

from urllib.parse import urlsplit

from rebrickable.errors import UnsafePaginationUrlError


def validate_next_url(next_url: str, base_url: str) -> str:
    candidate = urlsplit(next_url)
    base = urlsplit(base_url)
    if candidate.scheme != "https" or candidate.netloc != base.netloc:
        raise UnsafePaginationUrlError(
            "pagination URL left the configured HTTPS origin"
        )
    return next_url
