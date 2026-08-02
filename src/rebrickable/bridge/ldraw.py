"""Session facade for LDraw matching and BOM annotation."""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from filelock import FileLock

from rebrickable.bridge.mappings import (
    normalize_ldraw_code,
    resolve_ldraw_color,
    resolve_ldraw_part,
    resolve_rebrickable_color,
    resolve_rebrickable_part,
)
from rebrickable.bridge.models import (
    ColorMatch,
    LDrawBomItem,
    LDrawColorInfo,
    PartMatch,
    TranslatedBomRow,
    TranslationReport,
)
from rebrickable.bridge.overrides import read_overrides, write_overrides
from rebrickable.catalog.database import CatalogPaths
from rebrickable.errors import OptionalDependencyError
from rebrickable.types import MappingStatus

if TYPE_CHECKING:
    from pathlib import Path

    from rebrickable.api.client import RebrickableClient
    from rebrickable.session import RebrickableSession

_STATUS_SEVERITY = {
    MappingStatus.RESOLVED: 0,
    MappingStatus.AMBIGUOUS: 1,
    MappingStatus.UNRESOLVED: 2,
}


class LDrawBridge:
    def __init__(self, session: RebrickableSession) -> None:
        self._session = session

    async def resolve_ldraw_part(self, ldraw_code: str) -> PartMatch:
        return await resolve_ldraw_part(self._session, ldraw_code)

    async def resolve_rebrickable_part(self, part_num: str) -> PartMatch:
        return await resolve_rebrickable_part(self._session, part_num)

    async def find_ldraw_candidates(self, part_num: str) -> tuple[str, ...]:
        match = await self.resolve_rebrickable_part(part_num)
        if match.target_identifier:
            return (match.target_identifier,)
        return tuple(candidate.identifier for candidate in match.candidates)

    async def resolve_ldraw_color(self, color: LDrawColorInfo | int) -> ColorMatch:
        return await resolve_ldraw_color(self._session, color)

    async def resolve_rebrickable_color(self, color_id: int) -> ColorMatch:
        return await resolve_rebrickable_color(self._session, color_id)

    async def find_ldraw_color_candidates(self, color_id: int) -> tuple[int, ...]:
        match = await self.resolve_rebrickable_color(color_id)
        if match.target_identifier is not None:
            return (match.target_identifier,)
        return tuple(int(candidate.identifier) for candidate in match.candidates)

    async def enrich_part_mapping(
        self, part_num: str, client: RebrickableClient
    ) -> tuple[str, ...]:
        """Explicitly cache LDraw IDs reported by the API for one part."""
        from rebrickable.bridge.cache import store_crosswalks

        dto = await client.get_part(part_num, inc_part_details=1)
        values = _external_values(dto.external_ids, "ldraw")
        await asyncio.to_thread(
            store_crosswalks,
            self._session.config,
            entity_kind="part",
            external_system="ldraw",
            canonical_id=part_num,
            external_ids=values,
            operation_id="lego_parts_read",
            response_payload=dto.model_dump(mode="json"),
        )
        return values

    async def enrich_color_mapping(
        self, color_id: int, client: RebrickableClient
    ) -> tuple[str, ...]:
        """Explicitly cache LDraw IDs reported by the API for one color."""
        from rebrickable.bridge.cache import store_crosswalks

        dto = await client.get_color(color_id)
        values = _external_values(dto.external_ids, "ldraw")
        await asyncio.to_thread(
            store_crosswalks,
            self._session.config,
            entity_kind="color",
            external_system="ldraw",
            canonical_id=str(color_id),
            external_ids=values,
            operation_id="lego_colors_read",
            response_payload=dto.model_dump(mode="json"),
        )
        return values

    async def translate_bom(
        self,
        items: Iterable[LDrawBomItem],
        *,
        colors: dict[int, LDrawColorInfo] | None = None,
        diagnostics: tuple[str, ...] = (),
    ) -> TranslationReport:
        rows: list[TranslatedBomRow] = []
        for item in items:
            part_match = await self.resolve_ldraw_part(item.part_num)
            color_match = await self.resolve_ldraw_color(
                (colors or {}).get(item.color_code, item.color_code)
            )
            status = max(
                part_match.status,
                color_match.status,
                key=_STATUS_SEVERITY.__getitem__,
            )
            rows.append(
                TranslatedBomRow(
                    item.part_num,
                    item.color_code,
                    part_match.target_identifier,
                    color_match.target_identifier,
                    item.quantity,
                    status,
                    part_match,
                    color_match,
                ),
            )
        state = await self._session.state()
        snapshot = state.snapshot_id or "unknown"
        return TranslationReport(
            tuple(rows),
            sum(row.status is MappingStatus.RESOLVED for row in rows),
            sum(row.status is MappingStatus.AMBIGUOUS for row in rows),
            sum(row.status is MappingStatus.UNRESOLVED for row in rows),
            snapshot,
            diagnostics,
        )

    async def translate_model(
        self, model: object, *, parts: object | None = None
    ) -> TranslationReport:
        try:
            ldraw = importlib.import_module("ldraw")
        except ImportError as exc:
            raise OptionalDependencyError("install rebrickable[ldraw]") from exc
        rows = ldraw.bill_of_materials(
            cast("Any", model),
            parts=cast("Any", parts),
        )
        items = tuple(LDrawBomItem.from_pyldraw_bom(row) for row in rows)
        return await self.translate_bom(items, colors=_colors_from_parts(parts))

    async def translate_model_path(
        self,
        path: Path,
        *,
        parts: object | None = None,
        library_path: Path | None = None,
        tolerant: bool = True,
    ) -> TranslationReport:
        """Translate a model file.

        Without color metadata (a ``parts`` catalog or ``library_path`` to a
        ``parts.lst``), bare color codes resolve only through overrides or
        cached crosswalks.
        """
        try:
            ldraw = importlib.import_module("ldraw")
        except ImportError as exc:
            raise OptionalDependencyError("install rebrickable[ldraw]") from exc
        if parts is None and library_path is not None:
            parts = ldraw.Parts.get(library_path)
        result = ldraw.load_model(path, parts=cast("Any", parts), tolerant=tolerant)
        diagnostics = tuple(str(item) for item in getattr(result, "diagnostics", ()))
        if result.model is None:
            raise ValueError(
                "LDraw model could not be loaded: "
                + ("; ".join(diagnostics) or "unknown failure")
            )
        if not getattr(result, "complete", True):
            diagnostics = (*diagnostics, "model source is incomplete")
        rows = ldraw.bill_of_materials(
            cast("Any", result.model), parts=cast("Any", parts)
        )
        items = tuple(LDrawBomItem.from_pyldraw_bom(row) for row in rows)
        return await self.translate_bom(
            items, colors=_colors_from_parts(parts), diagnostics=diagnostics
        )

    async def annotate_pyldraw_bom(
        self, rows: Iterable[object], *, parts: object | None = None
    ) -> TranslationReport:
        """Translate public ``pyldraw3.BomRow`` objects without mutating them."""
        return await self.translate_bom(
            tuple(LDrawBomItem.from_pyldraw_bom(row) for row in rows),
            colors=_colors_from_parts(parts),
        )

    async def _record_override(
        self,
        *,
        entity_kind: str,
        source_id: str,
        target_id: str,
        reason: str,
    ) -> None:
        override_path = self._session.config.mapping_overrides_path
        if override_path is None:
            raise ValueError("mapping_overrides_path is not configured")
        state = await self._session.state()
        await asyncio.to_thread(
            _persist_override,
            path=override_path,
            lock_path=CatalogPaths.from_config(self._session.config).lock_file,
            database=state.database_path if state.database_path.is_file() else None,
            entity_kind=entity_kind,
            source_id=source_id,
            target_id=target_id,
            reason=reason,
        )

    async def record_part_override(
        self, ldraw_code: str, part_num: str, *, reason: str = ""
    ) -> None:
        await self._record_override(
            entity_kind="part",
            source_id=normalize_ldraw_code(ldraw_code),
            target_id=part_num,
            reason=reason,
        )

    async def record_color_override(
        self, ldraw_code: int, color_id: int, *, reason: str = ""
    ) -> None:
        await self._record_override(
            entity_kind="color",
            source_id=str(ldraw_code),
            target_id=str(color_id),
            reason=reason,
        )

    async def _remove_override(self, entity_kind: str, source_id: str) -> None:
        override_path = self._session.config.mapping_overrides_path
        if override_path is None:
            raise ValueError("mapping_overrides_path is not configured")
        state = await self._session.state()
        await asyncio.to_thread(
            _delete_override,
            override_path,
            CatalogPaths.from_config(self._session.config).lock_file,
            state.database_path if state.database_path.is_file() else None,
            entity_kind,
            source_id,
        )

    async def remove_part_override(self, ldraw_code: str) -> None:
        await self._remove_override("part", normalize_ldraw_code(ldraw_code))

    async def remove_color_override(self, ldraw_code: int) -> None:
        await self._remove_override("color", str(ldraw_code))


def _colors_from_parts(parts: object | None) -> dict[int, LDrawColorInfo]:
    """Build the LDraw color table from a pyldraw3 ``Parts`` catalog."""
    mapping = getattr(parts, "colours_by_code", None)
    if not isinstance(mapping, Mapping):
        return {}
    colors: dict[int, LDrawColorInfo] = {}
    for code, colour in mapping.items():
        try:
            colors[int(code)] = LDrawColorInfo.from_pyldraw_colour(colour)
        except (TypeError, ValueError):
            continue
    return colors


def _external_values(external_ids: object, system: str) -> tuple[str, ...]:
    if not isinstance(external_ids, Mapping):
        return ()
    for key, raw_value in external_ids.items():
        if str(key).casefold() != system.casefold():
            continue
        values = raw_value
        if isinstance(values, Mapping):
            values = values.get("ext_ids", values.get("ids", ()))
        if isinstance(values, str | int):
            values = (values,)
        if isinstance(values, list | tuple):
            return tuple(sorted({str(value) for value in values}))
    return ()


def _persist_override(
    *,
    path: Path,
    lock_path: Path,
    database: Path | None,
    entity_kind: str,
    source_id: str,
    target_id: str,
    reason: str,
) -> None:
    from rebrickable.bridge.cache import _update_search_document

    with FileLock(lock_path):
        records = [
            item
            for item in read_overrides(path)
            if not (
                item["entity_kind"] == entity_kind
                and item["source_system"] == "ldraw"
                and item["source_id"].casefold() == source_id.casefold()
                and item["target_system"] == "rebrickable"
            )
        ]
        records.append(
            {
                "entity_kind": entity_kind,
                "source_system": "ldraw",
                "source_id": source_id,
                "target_system": "rebrickable",
                "target_id": target_id,
                "reason": reason,
            }
        )
        write_overrides(path, tuple(records))
        if database is not None:
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO user_mapping_overrides VALUES (?, ?, ?, ?, ?, ?)",
                    (entity_kind, "ldraw", source_id, "rebrickable", target_id, reason),
                )
                _update_search_document(connection, entity_kind, target_id)
                connection.commit()
            finally:
                connection.close()


def _delete_override(
    path: Path,
    lock_path: Path,
    database: Path | None,
    entity_kind: str,
    source_id: str,
) -> None:
    from rebrickable.bridge.cache import _update_search_document

    with FileLock(lock_path):
        existing = read_overrides(path)
        affected = {
            item["target_id"]
            for item in existing
            if item["entity_kind"] == entity_kind
            and item["source_system"] == "ldraw"
            and item["source_id"].casefold() == source_id.casefold()
        }
        records = tuple(
            item
            for item in existing
            if not (
                item["entity_kind"] == entity_kind
                and item["source_system"] == "ldraw"
                and item["source_id"].casefold() == source_id.casefold()
            )
        )
        write_overrides(path, records)
        if database is not None:
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "DELETE FROM user_mapping_overrides WHERE entity_kind=? AND source_system='ldraw' AND lower(source_id)=?",
                    (entity_kind, source_id.casefold()),
                )
                for target_id in affected:
                    _update_search_document(connection, entity_kind, target_id)
                connection.commit()
            finally:
                connection.close()
