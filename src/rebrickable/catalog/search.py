"""Deterministic, safely escaped unified local search."""

from __future__ import annotations

import re

import aiosqlite

from rebrickable.catalog.traversal import THEME_SUBTREE_CTE
from rebrickable.types import SearchFilters, SearchHit, SearchKind, SearchResult


def _fts_query(query: str) -> str:
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


async def search(
    connection: aiosqlite.Connection,
    snapshot_id: str,
    query: str,
    *,
    kinds: set[SearchKind] | None = None,
    filters: SearchFilters | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    if not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    if offset < 0:
        raise ValueError("offset must not be negative")
    normalized = " ".join(query.casefold().split())
    active_filters = filters or SearchFilters()
    if active_filters.include_subthemes and active_filters.theme_id is None:
        raise ValueError("include_subthemes requires theme_id")
    if not normalized and kinds is None and active_filters == SearchFilters():
        raise ValueError("empty search requires a kind or filter")
    conditions: list[str] = []
    values: list[object] = []
    ctes: list[str] = []
    cte_values: list[object] = []
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        conditions.append(f"kind IN ({placeholders})")
        values.extend(item.value for item in sorted(kinds, key=lambda item: item.value))
    filter_map = {
        "year >= ?": active_filters.year_from,
        "year <= ?": active_filters.year_to,
        "num_parts >= ?": active_filters.min_parts,
        "num_parts <= ?": active_filters.max_parts,
        "category_id = ?": active_filters.category_id,
        "material = ?": active_filters.material,
    }
    for clause, value in filter_map.items():
        if value is not None:
            conditions.append(clause)
            values.append(value)
    if active_filters.theme_id is not None:
        if active_filters.include_subthemes:
            ctes.append(THEME_SUBTREE_CTE)
            cte_values.append(active_filters.theme_id)
            conditions.append("theme_id IN (SELECT id FROM theme_tree)")
        else:
            conditions.append("theme_id = ?")
            values.append(active_filters.theme_id)
    fts = _fts_query(normalized)
    if normalized:
        ctes.append(
            "fts_hits(rowid) AS MATERIALIZED "
            "(SELECT rowid FROM search_fts WHERE search_fts MATCH ?)"
        )
        cte_values.append(fts or '""')
        conditions.append(
            "(lower(canonical_id)=? OR instr(' '||lower(external_ids)||' ',' '||?||' ')>0 "
            "OR lower(canonical_id) LIKE ? ESCAPE '\\' OR normalized_name=? "
            "OR normalized_name LIKE ? ESCAPE '\\' "
            "OR rowid IN (SELECT rowid FROM fts_hits) "
            "OR normalized_name LIKE ? ESCAPE '\\')",
        )
        escaped = (
            normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        values.extend(
            (
                normalized,
                normalized,
                f"{escaped}%",
                normalized,
                f"{escaped}%",
                f"%{escaped}%",
            ),
        )
    cte_sql = ("WITH RECURSIVE " + ", ".join(ctes)) if ctes else ""
    where = " AND ".join(conditions) if conditions else "1"
    if normalized:
        rank_sql = """
        CASE
          WHEN lower(canonical_id)=? THEN 1
          WHEN instr(' '||lower(external_ids)||' ',' '||?||' ')>0 THEN 2
          WHEN lower(canonical_id) LIKE ? ESCAPE '\\' THEN 3
          WHEN normalized_name=? THEN 4
          WHEN normalized_name LIKE ? ESCAPE '\\' THEN 5
          WHEN rowid IN (SELECT rowid FROM fts_hits) THEN 6
          ELSE 7
        END
        """
        escaped = (
            normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        rank_values: list[object] = [
            normalized,
            normalized,
            f"{escaped}%",
            normalized,
            f"{escaped}%",
        ]
    else:
        rank_sql = "7"
        rank_values = []
    rows = list(
        await (
            await connection.execute(
                f"""
                {cte_sql}
                SELECT kind, canonical_id, title, subtitle, external_ids,
                       {rank_sql} AS rank,
                       COUNT(*) OVER () AS total
                FROM search_documents
                WHERE {where}
                ORDER BY rank, kind, canonical_id
                LIMIT ? OFFSET ?
                """,
                [*cte_values, *rank_values, *values, limit, offset],
            )
        ).fetchall()
    )
    if rows:
        total = int(rows[0]["total"])
    elif offset:
        count_row = await (
            await connection.execute(
                f"{cte_sql} SELECT COUNT(*) FROM search_documents WHERE {where}",
                [*cte_values, *values],
            )
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    else:
        total = 0
    hits: list[SearchHit] = []
    for row in rows:
        rank = int(row["rank"])
        if rank in {1, 3}:
            matched_field, matched_value = "canonical_id", row["canonical_id"]
        elif rank == 2:
            matched_field, matched_value = "external_id", normalized
        else:
            matched_field, matched_value = "name", row["title"]
        hits.append(
            SearchHit(
                SearchKind(row["kind"]),
                row["canonical_id"],
                row["title"],
                row["subtitle"],
                float(8 - rank),
                matched_field,
                matched_value,
            )
        )
    return SearchResult(tuple(hits), total, snapshot_id)
