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
            affected = {canonical_id}
            if external_ids:
                placeholders = ",".join("?" for _ in external_ids)
                stale = connection.execute(
                    "SELECT DISTINCT canonical_id FROM api_crosswalk_cache "
                    "WHERE entity_kind=? AND external_system=? "
                    f"AND external_id IN ({placeholders})",
                    (entity_kind, external_system, *external_ids),
                ).fetchall()
                affected.update(str(row[0]) for row in stale)
                connection.execute(
                    "DELETE FROM api_crosswalk_cache "
                    "WHERE entity_kind=? AND external_system=? "
                    f"AND external_id IN ({placeholders})",
                    (entity_kind, external_system, *external_ids),
                )
            connection.execute(
                "DELETE FROM api_crosswalk_cache "
                "WHERE entity_kind=? AND external_system=? AND canonical_id=?",
                (entity_kind, external_system, canonical_id),
            )
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
            for item in sorted(affected):
                _update_search_document(connection, entity_kind, item)
            connection.commit()
        finally:
            connection.close()


def _update_search_document(
    connection: sqlite3.Connection, entity_kind: str, canonical_id: str
) -> None:
    """Refresh one document's external ids and its FTS rows in place."""
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
    external = " ".join(str(row[0]) for row in rows)
    documents = connection.execute(
        "SELECT rowid, canonical_id, title, subtitle, external_ids "
        "FROM search_documents WHERE kind=? AND canonical_id=?",
        (entity_kind, canonical_id),
    ).fetchall()
    for document in documents:
        connection.execute(
            "INSERT INTO search_fts(search_fts, rowid, canonical_id, title,"
            " subtitle, external_ids) VALUES('delete', ?, ?, ?, ?, ?)",
            (document[0], document[1], document[2], document[3], document[4]),
        )
        connection.execute(
            "UPDATE search_documents SET external_ids=? WHERE rowid=?",
            (external, document[0]),
        )
        connection.execute(
            "INSERT INTO search_fts(rowid, canonical_id, title, subtitle,"
            " external_ids) VALUES(?, ?, ?, ?, ?)",
            (document[0], document[1], document[2], document[3], external),
        )
