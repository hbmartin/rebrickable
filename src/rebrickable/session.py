"""Offline session and repository composition root."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from typing import Self

import aiosqlite

from rebrickable.bom import Bom, BomValidationReport, BomValidationRow
from rebrickable.bridge.models import (
    ColorMatch,
    LDrawBomItem,
    MatchCandidate,
    PartMatch,
    TranslationReport,
)
from rebrickable.catalog.database import catalog_state, open_catalog
from rebrickable.catalog.queries import (
    CategoriesRepository,
    ColorsRepository,
    ElementsRepository,
    InventoriesRepository,
    MinifigsRepository,
    PartsRepository,
    SetsRepository,
    ThemesRepository,
)
from rebrickable.catalog.search import search as search_catalog
from rebrickable.config import Config
from rebrickable.errors import CatalogUnavailableError
from rebrickable.progress import ProgressCallback
from rebrickable.refresh import refresh_catalog
from rebrickable.types import (
    CatalogState,
    ColorRef,
    ColorSystem,
    ExternalIdentifier,
    MappingSource,
    MappingStatus,
    PartColorAvailability,
    PartRef,
    PartSystem,
    Provenance,
    ProvenanceSource,
    RefreshReport,
    RelationshipType,
    SearchFilters,
    SearchKind,
    SearchResult,
    SubstitutionEvidence,
)


class RebrickableSession:
    """A snapshot-pinned asynchronous local catalog session."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._db: aiosqlite.Connection | None = None
        self._opened_state: CatalogState | None = None
        self._open_lock = asyncio.Lock()
        self.parts = PartsRepository(self)
        self.sets = SetsRepository(self)
        self.minifigs = MinifigsRepository(self)
        self.colors = ColorsRepository(self)
        self.themes = ThemesRepository(self)
        self.categories = CategoriesRepository(self)
        self.elements = ElementsRepository(self)
        self.inventories = InventoriesRepository(self)
        from rebrickable.bridge.ldraw import LDrawBridge

        self.ldraw = LDrawBridge(self)

    @classmethod
    async def open(cls, config: Config | None = None) -> RebrickableSession:
        """Create a lazy session; catalog access opens and pins the snapshot."""
        return cls(Config.load() if config is None else config)

    @classmethod
    async def connect(cls, config: Config | None = None) -> RebrickableSession:
        """Create a session and eagerly verify/open its pinned catalog snapshot."""
        session = cls(Config.load() if config is None else config)
        try:
            await session._connection()
        except BaseException:
            await session.close()
            raise
        return session

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            async with self._open_lock:
                if self._db is None:
                    self._db, self._opened_state = await open_catalog(self.config)
        return self._db

    async def _pinned_state(self) -> CatalogState:
        await self._connection()
        state = self._opened_state
        if state is None:  # pragma: no cover - _connection always sets it
            raise CatalogUnavailableError("opened catalog has no state")
        return state

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._opened_state = None

    async def state(self, *, verify: bool = False) -> CatalogState:
        """Return the currently active catalog state, which may differ when pinned."""
        return await catalog_state(self.config, verify=verify)

    async def current_catalog_state(self, *, verify: bool = False) -> CatalogState:
        """Explicit spelling for the current active catalog pointer."""
        return await self.state(verify=verify)

    async def opened_catalog_state(self) -> CatalogState:
        """Return the immutable catalog state pinned by this open session."""
        return await self._pinned_state()

    async def refresh(
        self,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RefreshReport:
        report = await refresh_catalog(self.config, force=force, callback=progress)
        await self.close()
        return report

    async def refresh_catalog(
        self,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RefreshReport:
        """Compatibility spelling for :meth:`refresh`."""
        return await self.refresh(force=force, progress=progress)

    async def search(
        self,
        query: str,
        *,
        kinds: set[SearchKind] | None = None,
        filters: SearchFilters | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SearchResult:
        connection = await self._connection()
        state = self._opened_state
        if state is None or state.snapshot_id is None:  # pragma: no cover
            raise AssertionError("opened catalog has no snapshot")
        return await search_catalog(
            connection,
            state.snapshot_id,
            query,
            kinds=kinds,
            filters=filters,
            limit=limit,
            offset=offset,
        )

    async def check_part_color(
        self,
        part: PartRef,
        color: ColorRef,
    ) -> PartColorAvailability:
        resolved = await self._resolve_part_color(part, color)
        return (await self._check_resolved_part_colors((resolved,)))[0]

    async def _resolve_part_color(
        self, part: PartRef, color: ColorRef
    ) -> tuple[str | None, int | None, str]:
        if part.system is PartSystem.REBRICKABLE:
            part_num = part.value
            part_confidence = "exact"
        elif part.system is PartSystem.LDRAW:
            match = await self.ldraw.resolve_ldraw_part(part.value)
            part_num = match.target_identifier
            part_confidence = match.source.value
        else:
            part_num = None
            part_confidence = "none"
        if color.system is ColorSystem.REBRICKABLE:
            color_id = color.value
            color_confidence = "exact"
        elif color.system is ColorSystem.LDRAW:
            match_color = await self.ldraw.resolve_ldraw_color(color.value)
            color_id = match_color.target_identifier
            color_confidence = match_color.source.value
        else:
            color_id = None
            color_confidence = "none"
        return part_num, color_id, f"{part_confidence}/{color_confidence}"

    async def _check_resolved_part_colors(
        self,
        resolved: tuple[tuple[str | None, int | None, str], ...],
    ) -> tuple[PartColorAvailability, ...]:
        if not resolved:
            return ()
        connection = await self._connection()
        element_ids: dict[int, list[str]] = {}
        evidence_by_index: dict[int, aiosqlite.Row] = {}
        relationship_only: set[int] = set()
        for start in range(0, len(resolved), 300):
            chunk = resolved[start : start + 300]
            valid = [
                (start + index, part_num, color_id)
                for index, (part_num, color_id, _confidence) in enumerate(chunk)
                if part_num is not None and color_id is not None
            ]
            if not valid:
                continue
            placeholders = ",".join("(?,?,?)" for _ in valid)
            parameters = [value for item in valid for value in item]
            requested = (
                f"WITH requested(idx, part_num, color_id) AS (VALUES {placeholders})"
            )
            element_rows = await (
                await connection.execute(
                    requested + " SELECT r.idx, e.element_id FROM requested r "
                    "JOIN elements e ON e.part_num=r.part_num AND e.color_id=r.color_id "
                    "ORDER BY r.idx, e.element_id",
                    parameters,
                )
            ).fetchall()
            for row in element_rows:
                element_ids.setdefault(int(row["idx"]), []).append(
                    str(row["element_id"])
                )
            evidence_rows = await (
                await connection.execute(
                    requested
                    + """
                    SELECT r.idx, COUNT(DISTINCT s.set_num) AS set_count,
                           COUNT(DISTINCT li.id) AS inventory_count,
                           MIN(s.year) AS first_year, MAX(s.year) AS last_year,
                           MAX(CASE WHEN li.id IS NOT NULL THEN ip.is_spare END)
                               AS any_spare,
                           MIN(CASE WHEN li.id IS NOT NULL THEN ip.is_spare END)
                               AS all_spare
                    FROM requested r
                    LEFT JOIN inventory_parts ip
                      ON ip.part_num=r.part_num AND ip.color_id=r.color_id
                    LEFT JOIN latest_inventories li ON li.id=ip.inventory_id
                    LEFT JOIN sets s ON s.set_num=li.owner_num
                    GROUP BY r.idx ORDER BY r.idx
                    """,
                    parameters,
                )
            ).fetchall()
            evidence_by_index.update((int(row["idx"]), row) for row in evidence_rows)
            unavailable = [
                item
                for item in valid
                if not element_ids.get(item[0])
                and not int(evidence_by_index[item[0]]["inventory_count"] or 0)
            ]
            if unavailable:
                unavailable_placeholders = ",".join("(?,?,?)" for _ in unavailable)
                unavailable_parameters = [
                    value for item in unavailable for value in item
                ]
                relationship_rows = await (
                    await connection.execute(
                        "WITH requested(idx, part_num, color_id) AS (VALUES "
                        + unavailable_placeholders
                        + ") "
                        + """
                        SELECT DISTINCT r.idx
                        FROM requested r
                        JOIN part_relationships pr
                          ON pr.child_part_num=r.part_num OR pr.parent_part_num=r.part_num
                        LEFT JOIN elements e
                          ON e.part_num=CASE WHEN pr.child_part_num=r.part_num
                                             THEN pr.parent_part_num
                                             ELSE pr.child_part_num END
                         AND e.color_id=r.color_id
                        LEFT JOIN inventory_parts ip
                          ON ip.part_num=CASE WHEN pr.child_part_num=r.part_num
                                              THEN pr.parent_part_num
                                              ELSE pr.child_part_num END
                         AND ip.color_id=r.color_id
                        LEFT JOIN latest_inventories li ON li.id=ip.inventory_id
                        WHERE e.element_id IS NOT NULL OR li.id IS NOT NULL
                        """,
                        unavailable_parameters,
                    )
                ).fetchall()
                relationship_only.update(int(row["idx"]) for row in relationship_rows)

        state = self._opened_state
        provenance = (
            Provenance(
                ProvenanceSource.LOCAL_SNAPSHOT,
                state.snapshot_id if state and state.snapshot_id else "unknown",
            ),
        )
        results: list[PartColorAvailability] = []
        for index, (part_num, color_id, confidence) in enumerate(resolved):
            if part_num is None or color_id is None:
                results.append(
                    PartColorAvailability(False, part_num, color_id, confidence="none")
                )
                continue
            evidence = evidence_by_index[index]
            elements = tuple(element_ids.get(index, ()))
            inventory_count = int(evidence["inventory_count"] or 0)
            results.append(
                PartColorAvailability(
                    available=bool(elements) or inventory_count > 0,
                    rebrickable_part=part_num,
                    rebrickable_color=color_id,
                    element_ids=elements,
                    set_count=int(evidence["set_count"] or 0),
                    inventory_count=inventory_count,
                    first_year=evidence["first_year"],
                    last_year=evidence["last_year"],
                    appears_as_spare=bool(evidence["any_spare"]),
                    spare_only=(
                        bool(evidence["all_spare"]) if inventory_count else False
                    ),
                    relationship_only=index in relationship_only,
                    confidence=confidence,
                    provenance=provenance,
                )
            )
        return tuple(results)

    async def resolve_part(self, part: PartRef) -> PartMatch:
        """Resolve a namespaced part reference without implicit API access."""
        if part.system is PartSystem.LDRAW:
            return await self.ldraw.resolve_ldraw_part(part.value)
        state = await self._pinned_state()
        snapshot = state.snapshot_id or "unknown"
        if part.system is PartSystem.REBRICKABLE:
            entity = await self.parts.get(part.value)
            return PartMatch(
                part.value,
                part.value if entity is not None else None,
                MappingStatus.RESOLVED
                if entity is not None
                else MappingStatus.UNRESOLVED,
                MappingSource.EXACT_CANONICAL_ID
                if entity is not None
                else MappingSource.NONE,
                1.0 if entity is not None else 0.0,
                (),
                "canonical Rebrickable identifier"
                if entity is not None
                else "part is absent from the active snapshot",
                snapshot,
            )
        connection = await self._connection()
        rows = tuple(
            await (
                await connection.execute(
                    """
                    SELECT canonical_id FROM api_crosswalk_cache
                    WHERE entity_kind='part' AND external_system=? AND external_id=?
                    ORDER BY canonical_id
                    """,
                    (part.system.value, part.value),
                )
            ).fetchall()
        )
        candidates = tuple(
            MatchCandidate(str(row[0]), None, "API-confirmed external ID", 1.0)
            for row in rows
        )
        return PartMatch(
            part.value,
            str(rows[0][0]) if len(rows) == 1 else None,
            MappingStatus.RESOLVED
            if len(rows) == 1
            else MappingStatus.AMBIGUOUS
            if rows
            else MappingStatus.UNRESOLVED,
            MappingSource.API_EXTERNAL_ID if rows else MappingSource.NONE,
            1.0 if len(rows) == 1 else 0.0,
            () if len(rows) == 1 else candidates,
            "API-confirmed external identifier"
            if len(rows) == 1
            else "external identifier is ambiguous"
            if rows
            else "no offline mapping",
            snapshot,
        )

    async def resolve_color(self, color: ColorRef) -> ColorMatch:
        """Resolve a namespaced color reference without implicit API access."""
        if color.system is ColorSystem.LDRAW:
            return await self.ldraw.resolve_ldraw_color(color.value)
        state = await self._pinned_state()
        snapshot = state.snapshot_id or "unknown"
        if color.system is ColorSystem.REBRICKABLE:
            entity = await self.colors.get(color.value)
            return ColorMatch(
                color.value,
                color.value if entity is not None else None,
                MappingStatus.RESOLVED
                if entity is not None
                else MappingStatus.UNRESOLVED,
                MappingSource.EXACT_CANONICAL_ID
                if entity is not None
                else MappingSource.NONE,
                1.0 if entity is not None else 0.0,
                (),
                "canonical Rebrickable identifier"
                if entity is not None
                else "color is absent from the active snapshot",
                snapshot,
            )
        connection = await self._connection()
        rows = tuple(
            await (
                await connection.execute(
                    """
                    SELECT canonical_id FROM api_crosswalk_cache
                    WHERE entity_kind='color' AND external_system=? AND external_id=?
                    ORDER BY CAST(canonical_id AS INTEGER)
                    """,
                    (color.system.value, str(color.value)),
                )
            ).fetchall()
        )
        candidates = tuple(
            MatchCandidate(str(row[0]), None, "API-confirmed external ID", 1.0)
            for row in rows
        )
        return ColorMatch(
            color.value,
            int(rows[0][0]) if len(rows) == 1 else None,
            MappingStatus.RESOLVED
            if len(rows) == 1
            else MappingStatus.AMBIGUOUS
            if rows
            else MappingStatus.UNRESOLVED,
            MappingSource.API_EXTERNAL_ID if rows else MappingSource.NONE,
            1.0 if len(rows) == 1 else 0.0,
            () if len(rows) == 1 else candidates,
            "API-confirmed external identifier"
            if len(rows) == 1
            else "external identifier is ambiguous"
            if rows
            else "no offline mapping",
            snapshot,
        )

    async def external_ids(self, part: PartRef) -> tuple[ExternalIdentifier, ...]:
        """Return locally cached or overridden external IDs for a canonical part."""
        match = await self.resolve_part(part)
        canonical_id = match.target_identifier
        if canonical_id is None:
            return ()
        connection = await self._connection()
        state = self._opened_state
        snapshot = state.snapshot_id if state and state.snapshot_id else "unknown"
        cached = tuple(
            await (
                await connection.execute(
                    """
                    SELECT external_system, external_id, operation_id, retrieved_at
                    FROM api_crosswalk_cache
                    WHERE entity_kind='part' AND canonical_id=?
                    ORDER BY external_system, external_id
                    """,
                    (canonical_id,),
                )
            ).fetchall()
        )
        overridden = tuple(
            await (
                await connection.execute(
                    """
                    SELECT source_system, source_id FROM user_mapping_overrides
                    WHERE entity_kind='part' AND target_system='rebrickable' AND target_id=?
                    ORDER BY source_system, source_id
                    """,
                    (canonical_id,),
                )
            ).fetchall()
        )
        values = [
            ExternalIdentifier(
                str(row[0]),
                str(row[1]),
                Provenance(ProvenanceSource.API_OPERATION, str(row[2])),
            )
            for row in cached
        ]
        values.extend(
            ExternalIdentifier(
                str(row[0]),
                str(row[1]),
                Provenance(ProvenanceSource.USER_OVERRIDE, snapshot),
            )
            for row in overridden
        )
        return tuple(values)

    async def substitutes(
        self,
        part: PartRef,
        *,
        direction: str = "both",
        include_mold_variants: bool = True,
        include_prints: bool = False,
        max_depth: int = 4,
        max_candidates: int = 100,
    ) -> tuple[SubstitutionEvidence, ...]:
        """Traverse the local relationship graph and retain why each candidate exists.

        Traversal stops ``max_depth`` hops from the root and the evidence list
        is truncated at ``max_candidates`` entries.
        """
        if direction not in {"both", "parents", "children"}:
            raise ValueError("direction must be both, parents, or children")
        if max_depth < 0:
            raise ValueError("max_depth must not be negative")
        if max_candidates < 0:
            raise ValueError("max_candidates must not be negative")
        match = await self.resolve_part(part)
        if match.target_identifier is None:
            return ()
        root = match.target_identifier
        connection = await self._connection()
        state = self._opened_state
        snapshot = state.snapshot_id if state and state.snapshot_id else "unknown"
        pending: deque[tuple[str, tuple[str, ...], int]] = deque([(root, (root,), 0)])
        seen = {root}
        results: list[SubstitutionEvidence] = []
        while pending and len(results) < max_candidates:
            current, path, depth = pending.popleft()
            if depth >= max_depth:
                continue
            clauses: list[str] = []
            values: list[str] = []
            if direction in {"both", "parents"}:
                clauses.append("child_part_num=?")
                values.append(current)
            if direction in {"both", "children"}:
                clauses.append("parent_part_num=?")
                values.append(current)
            rows = await (
                await connection.execute(
                    "SELECT rel_type, child_part_num, parent_part_num "
                    f"FROM part_relationships WHERE {' OR '.join(clauses)} "
                    "ORDER BY rel_type, child_part_num, parent_part_num",
                    values,
                )
            ).fetchall()
            for row in rows:
                raw = str(row[0])
                relationship = RelationshipType.from_upstream(raw)
                if not include_prints and relationship in {
                    RelationshipType.PRINT,
                    RelationshipType.PATTERN,
                }:
                    continue
                if not include_mold_variants and relationship is RelationshipType.MOLD:
                    continue
                candidate = str(row[2]) if str(row[1]) == current else str(row[1])
                if candidate in seen:
                    continue
                seen.add(candidate)
                candidate_path = (*path, candidate)
                pending.append((candidate, candidate_path, depth + 1))
                results.append(
                    SubstitutionEvidence(
                        PartRef(PartSystem.REBRICKABLE, root),
                        PartRef(PartSystem.REBRICKABLE, candidate),
                        relationship,
                        raw,
                        tuple(
                            PartRef(PartSystem.REBRICKABLE, value)
                            for value in candidate_path
                        ),
                        relationship
                        in {RelationshipType.PRINT, RelationshipType.PATTERN},
                        relationship is RelationshipType.SUB_PART,
                        "relationship-derived candidate; physical interchangeability is not guaranteed",
                        Provenance(ProvenanceSource.LOCAL_SNAPSHOT, snapshot),
                    )
                )
                if len(results) >= max_candidates:
                    break
        return tuple(results)

    async def translate_bom(self, items: Iterable[LDrawBomItem]) -> TranslationReport:
        """Forward to the neutral LDraw bridge for consumer convenience."""
        return await self.ldraw.translate_bom(items)

    async def validate_bom(self, bom: Bom) -> BomValidationReport:
        resolved: list[tuple[str | None, int | None, str]] = []
        for item in bom.items:
            resolved.append(await self._resolve_part_color(item.part, item.color))
        availability = await self._check_resolved_part_colors(tuple(resolved))
        rows: list[BomValidationRow] = []
        for item, result in zip(bom.items, availability, strict=True):
            notes = ["spare only"] if result.spare_only else []
            substitutions: tuple[SubstitutionEvidence, ...] = ()
            if (
                result.relationship_only
                and result.rebrickable_part is not None
                and result.rebrickable_color is not None
            ):
                candidates = await self.substitutes(
                    PartRef(PartSystem.REBRICKABLE, result.rebrickable_part)
                )
                usable: list[SubstitutionEvidence] = []
                for candidate in candidates:
                    availability = await self.check_part_color(
                        candidate.candidate,
                        ColorRef(ColorSystem.REBRICKABLE, result.rebrickable_color),
                    )
                    if availability.available:
                        usable.append(candidate)
                substitutions = tuple(usable)
                if substitutions:
                    notes.append("relationship-only substitute available")
            rows.append(
                BomValidationRow(
                    item,
                    result.available,
                    result.rebrickable_part,
                    result.rebrickable_color,
                    result.confidence,
                    tuple(notes),
                    substitutions,
                ),
            )
        return BomValidationReport(
            tuple(rows),
            bom.total_quantity,
            bom.unique_count,
            sum(row.available and row.confidence.startswith("exact/") for row in rows),
            sum(not row.available for row in rows),
            sum(bool(row.substitutions) for row in rows),
        )
