"""Serialized materialization of API-confirmed external identifier mappings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from filelock import FileLock

from rebrickable.catalog.database import CatalogPaths
from rebrickable.config import Config
from rebrickable.errors import CatalogUnavailableError


def store_crosswalks(
    config: Config,
    *,
    entity_kind: str,
    external_system: str,
    canonical_id: str,
    external_ids: tuple[str, ...],
    operation_id: str,
    response_payload: object,
) -> None:
    """Write confirmed identifiers under the same lock used for promotion."""
    paths = CatalogPaths.from_config(config)
    try:
        pointer = json.loads(paths.active_pointer.read_text(encoding="utf-8"))
        snapshot_id = str(pointer["snapshot_id"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CatalogUnavailableError("catalog is not ready") from exc
    database = paths.database_for(snapshot_id)
    if not database.is_file():
        raise CatalogUnavailableError("catalog is not ready")
    payload = json.dumps(
        response_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    retrieved_at = datetime.now(UTC).isoformat()
    lock = FileLock(paths.lock_file)
    with lock:
        connection = sqlite3.connect(database)
        try:
            connection.executemany(
                """
                INSERT OR REPLACE INTO api_crosswalk_cache
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        entity_kind,
                        external_system,
                        external_id,
                        canonical_id,
                        operation_id,
                        retrieved_at,
                        digest,
                    )
                    for external_id in external_ids
                ),
            )
            _update_search_document(connection, entity_kind, canonical_id)
            connection.execute("INSERT INTO search_fts(search_fts) VALUES('rebuild')")
            connection.commit()
        finally:
            connection.close()


def _update_search_document(
    connection: sqlite3.Connection, entity_kind: str, canonical_id: str
) -> None:
    rows = connection.execute(
        """
        SELECT external_id FROM api_crosswalk_cache
        WHERE entity_kind=? AND canonical_id=?
        UNION
        SELECT source_id FROM user_mapping_overrides
        WHERE entity_kind=? AND target_system='rebrickable' AND target_id=?
        ORDER BY 1
        """,
        (entity_kind, canonical_id, entity_kind, canonical_id),
    ).fetchall()
    connection.execute(
        "UPDATE search_documents SET external_ids=? WHERE kind=? AND canonical_id=?",
        (" ".join(str(row[0]) for row in rows), entity_kind, canonical_id),
    )
