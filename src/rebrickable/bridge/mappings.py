"""Deterministic offline LDraw/Rebrickable matching strategies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rebrickable.bridge.models import (
    ColorMatch,
    LDrawColorInfo,
    MatchCandidate,
    PartMatch,
)
from rebrickable.types import MappingSource, MappingStatus, normalize_ldraw_code

if TYPE_CHECKING:
    from rebrickable.bridge._session import BridgeSession

__all__ = [
    "normalize_ldraw_code",
    "resolve_ldraw_color",
    "resolve_ldraw_part",
    "resolve_rebrickable_color",
    "resolve_rebrickable_part",
]


async def resolve_ldraw_part(session: BridgeSession, ldraw_code: str) -> PartMatch:
    connection = await session._connection()
    state = session._opened_state
    snapshot = state.snapshot_id if state and state.snapshot_id else "unknown"
    code = normalize_ldraw_code(ldraw_code)
    override = await (
        await connection.execute(
            """
            SELECT target_id FROM user_mapping_overrides
            WHERE entity_kind='part' AND source_system='ldraw'
              AND source_id=? COLLATE NOCASE AND target_system='rebrickable'
            """,
            (code,),
        )
    ).fetchone()
    if override:
        return PartMatch(
            source_identifier=ldraw_code,
            target_identifier=override[0],
            status=MappingStatus.RESOLVED,
            source=MappingSource.USER_OVERRIDE,
            confidence=1.0,
            candidates=(),
            explanation="explicit user override",
            snapshot_id=snapshot,
        )
    cached = tuple(
        await (
            await connection.execute(
                """
            SELECT canonical_id FROM api_crosswalk_cache
            WHERE entity_kind='part' AND external_system='ldraw'
              AND external_id=? COLLATE NOCASE
            ORDER BY canonical_id
            """,
                (code,),
            )
        ).fetchall()
    )
    if len(cached) == 1:
        return PartMatch(
            source_identifier=ldraw_code,
            target_identifier=cached[0][0],
            status=MappingStatus.RESOLVED,
            source=MappingSource.API_EXTERNAL_ID,
            confidence=1.0,
            candidates=(),
            explanation="API-confirmed external identifier",
            snapshot_id=snapshot,
        )
    if cached:
        return PartMatch(
            source_identifier=ldraw_code,
            target_identifier=None,
            status=MappingStatus.AMBIGUOUS,
            source=MappingSource.API_EXTERNAL_ID,
            confidence=0.0,
            candidates=tuple(
                MatchCandidate(
                    identifier=str(row[0]),
                    name=None,
                    reason="API-confirmed external identifier",
                    confidence=1.0,
                )
                for row in cached
            ),
            explanation="multiple API-confirmed canonical identifiers",
            snapshot_id=snapshot,
        )
    exact = await (
        await connection.execute(
            "SELECT part_num, name FROM parts WHERE part_num=? COLLATE NOCASE", (code,)
        )
    ).fetchone()
    if exact:
        return PartMatch(
            source_identifier=ldraw_code,
            target_identifier=exact[0],
            status=MappingStatus.RESOLVED,
            source=MappingSource.EXACT_CANONICAL_ID,
            confidence=0.98,
            candidates=(),
            explanation="normalized identifiers are equal",
            snapshot_id=snapshot,
        )
    relationships = tuple(
        await (
            await connection.execute(
                """
            SELECT DISTINCT p.part_num, p.name, r.rel_type
            FROM part_relationships r
            JOIN parts p ON p.part_num = CASE WHEN lower(r.child_part_num)=? THEN r.parent_part_num ELSE r.child_part_num END
            WHERE lower(r.child_part_num)=? OR lower(r.parent_part_num)=?
            ORDER BY p.part_num
            """,
                (code, code, code),
            )
        ).fetchall()
    )
    candidates = tuple(
        MatchCandidate(
            identifier=row[0],
            name=row[1],
            reason=f"relationship {row[2]}",
            confidence=0.6,
        )
        for row in relationships
    )
    status = MappingStatus.AMBIGUOUS if candidates else MappingStatus.UNRESOLVED
    return PartMatch(
        source_identifier=ldraw_code,
        target_identifier=None,
        status=status,
        source=MappingSource.RELATIONSHIP_CANDIDATE
        if candidates
        else MappingSource.NONE,
        confidence=0.0,
        candidates=candidates,
        explanation="relationship candidates require review"
        if candidates
        else "no offline mapping",
        snapshot_id=snapshot,
    )


async def resolve_rebrickable_part(session: BridgeSession, part_num: str) -> PartMatch:
    connection = await session._connection()
    state = session._opened_state
    snapshot = state.snapshot_id if state and state.snapshot_id else "unknown"
    override = tuple(
        await (
            await connection.execute(
                "SELECT source_id FROM user_mapping_overrides WHERE entity_kind='part' AND target_system='rebrickable' AND target_id=? AND source_system='ldraw' ORDER BY source_id",
                (part_num,),
            )
        ).fetchall()
    )
    if len(override) == 1:
        return PartMatch(
            source_identifier=part_num,
            target_identifier=override[0][0],
            status=MappingStatus.RESOLVED,
            source=MappingSource.USER_OVERRIDE,
            confidence=1.0,
            candidates=(),
            explanation="explicit user override",
            snapshot_id=snapshot,
        )
    if override:
        return PartMatch(
            source_identifier=part_num,
            target_identifier=None,
            status=MappingStatus.AMBIGUOUS,
            source=MappingSource.USER_OVERRIDE,
            confidence=0.0,
            candidates=tuple(
                MatchCandidate(
                    identifier=str(row[0]),
                    name=None,
                    reason="user override",
                    confidence=1.0,
                )
                for row in override
            ),
            explanation="multiple user overrides map to this part",
            snapshot_id=snapshot,
        )
    cached = tuple(
        await (
            await connection.execute(
                "SELECT external_id FROM api_crosswalk_cache WHERE entity_kind='part' AND external_system='ldraw' AND canonical_id=? ORDER BY external_id",
                (part_num,),
            )
        ).fetchall()
    )
    if len(cached) == 1:
        return PartMatch(
            source_identifier=part_num,
            target_identifier=cached[0][0],
            status=MappingStatus.RESOLVED,
            source=MappingSource.API_EXTERNAL_ID,
            confidence=1.0,
            candidates=(),
            explanation="API-confirmed external identifier",
            snapshot_id=snapshot,
        )
    if cached:
        return PartMatch(
            source_identifier=part_num,
            target_identifier=None,
            status=MappingStatus.AMBIGUOUS,
            source=MappingSource.API_EXTERNAL_ID,
            confidence=0.0,
            candidates=tuple(
                MatchCandidate(
                    identifier=str(row[0]),
                    name=None,
                    reason="API-confirmed external identifier",
                    confidence=1.0,
                )
                for row in cached
            ),
            explanation="multiple API-confirmed external identifiers",
            snapshot_id=snapshot,
        )
    exact = await (
        await connection.execute(
            "SELECT part_num, name FROM parts WHERE part_num=?", (part_num,)
        )
    ).fetchone()
    if exact:
        return PartMatch(
            source_identifier=part_num,
            target_identifier=None,
            status=MappingStatus.AMBIGUOUS,
            source=MappingSource.EXACT_CANONICAL_ID,
            confidence=0.0,
            candidates=(
                MatchCandidate(
                    identifier=normalize_ldraw_code(part_num),
                    name=exact[1],
                    reason="same-identifier heuristic; LDraw existence unverified",
                    confidence=0.5,
                ),
            ),
            explanation="identifier exists in Rebrickable; confirm with enrich_part_mapping"
            " or an override",
            snapshot_id=snapshot,
        )
    return PartMatch(
        source_identifier=part_num,
        target_identifier=None,
        status=MappingStatus.UNRESOLVED,
        source=MappingSource.NONE,
        confidence=0.0,
        candidates=(),
        explanation="no offline mapping",
        snapshot_id=snapshot,
    )


async def resolve_ldraw_color(
    session: BridgeSession, color: LDrawColorInfo | int
) -> ColorMatch:
    connection = await session._connection()
    state = session._opened_state
    snapshot = state.snapshot_id if state and state.snapshot_id else "unknown"
    code = color if isinstance(color, int) else color.code
    override = await (
        await connection.execute(
            "SELECT target_id FROM user_mapping_overrides WHERE entity_kind='color' AND source_system='ldraw' AND source_id=? AND target_system='rebrickable'",
            (str(code),),
        )
    ).fetchone()
    if override:
        return ColorMatch(
            source_identifier=code,
            target_identifier=int(override[0]),
            status=MappingStatus.RESOLVED,
            source=MappingSource.USER_OVERRIDE,
            confidence=1.0,
            candidates=(),
            explanation="explicit user override",
            snapshot_id=snapshot,
        )
    cached = tuple(
        await (
            await connection.execute(
                "SELECT canonical_id FROM api_crosswalk_cache WHERE entity_kind='color' AND external_system='ldraw' AND external_id=? ORDER BY canonical_id",
                (str(code),),
            )
        ).fetchall()
    )
    if len(cached) == 1:
        return ColorMatch(
            source_identifier=code,
            target_identifier=int(cached[0][0]),
            status=MappingStatus.RESOLVED,
            source=MappingSource.API_EXTERNAL_ID,
            confidence=1.0,
            candidates=(),
            explanation="API-confirmed external identifier",
            snapshot_id=snapshot,
        )
    if cached:
        return ColorMatch(
            source_identifier=code,
            target_identifier=None,
            status=MappingStatus.AMBIGUOUS,
            source=MappingSource.API_EXTERNAL_ID,
            confidence=0.0,
            candidates=tuple(
                MatchCandidate(
                    identifier=str(row[0]),
                    name=None,
                    reason="API-confirmed external identifier",
                    confidence=1.0,
                )
                for row in cached
            ),
            explanation="multiple API-confirmed canonical identifiers",
            snapshot_id=snapshot,
        )
    if isinstance(color, int):
        return ColorMatch(
            source_identifier=code,
            target_identifier=None,
            status=MappingStatus.UNRESOLVED,
            source=MappingSource.NONE,
            confidence=0.0,
            candidates=(),
            explanation="color metadata is required for offline matching",
            snapshot_id=snapshot,
        )
    rgb = color.rgb.removeprefix("#").upper()
    transparent = color.alpha < 255
    rgb_rows = tuple(
        await (
            await connection.execute(
                "SELECT id, name FROM colors WHERE rgb=? AND is_trans=? ORDER BY id",
                (rgb, int(transparent)),
            )
        ).fetchall()
    )
    if len(rgb_rows) == 1:
        return ColorMatch(
            source_identifier=code,
            target_identifier=int(rgb_rows[0][0]),
            status=MappingStatus.RESOLVED,
            source=MappingSource.RGB,
            confidence=0.9,
            candidates=(),
            explanation="unique RGB and transparency match",
            snapshot_id=snapshot,
        )
    name_rows = tuple(
        await (
            await connection.execute(
                "SELECT id, name FROM colors WHERE lower(replace(name,'_',' '))=? AND is_trans=? ORDER BY id",
                (color.name.casefold().replace("_", " "), int(transparent)),
            )
        ).fetchall()
    )
    if len(name_rows) == 1:
        return ColorMatch(
            source_identifier=code,
            target_identifier=int(name_rows[0][0]),
            status=MappingStatus.RESOLVED,
            source=MappingSource.NAME,
            confidence=0.8,
            candidates=(),
            explanation="unique normalized name and transparency match",
            snapshot_id=snapshot,
        )
    rows = rgb_rows or name_rows
    candidates = tuple(
        MatchCandidate(
            identifier=str(row[0]),
            name=row[1],
            reason="color candidate",
            confidence=0.6,
        )
        for row in rows
    )
    return ColorMatch(
        source_identifier=code,
        target_identifier=None,
        status=MappingStatus.AMBIGUOUS if candidates else MappingStatus.UNRESOLVED,
        source=MappingSource.RGB
        if rgb_rows
        else MappingSource.NAME
        if name_rows
        else MappingSource.NONE,
        confidence=0.0,
        candidates=candidates,
        explanation="multiple color candidates"
        if candidates
        else "no offline color mapping",
        snapshot_id=snapshot,
    )


async def resolve_rebrickable_color(
    session: BridgeSession, color_id: int
) -> ColorMatch:
    connection = await session._connection()
    state = session._opened_state
    snapshot = state.snapshot_id if state and state.snapshot_id else "unknown"
    override = tuple(
        await (
            await connection.execute(
                """
                SELECT source_id FROM user_mapping_overrides
                WHERE entity_kind='color' AND source_system='ldraw'
                  AND target_system='rebrickable' AND target_id=?
                ORDER BY CAST(source_id AS INTEGER)
                """,
                (str(color_id),),
            )
        ).fetchall()
    )
    if len(override) == 1:
        return ColorMatch(
            source_identifier=color_id,
            target_identifier=int(override[0][0]),
            status=MappingStatus.RESOLVED,
            source=MappingSource.USER_OVERRIDE,
            confidence=1.0,
            candidates=(),
            explanation="explicit user override",
            snapshot_id=snapshot,
        )
    cached = tuple(
        await (
            await connection.execute(
                """
                SELECT external_id FROM api_crosswalk_cache
                WHERE entity_kind='color' AND external_system='ldraw'
                  AND canonical_id=? ORDER BY CAST(external_id AS INTEGER)
                """,
                (str(color_id),),
            )
        ).fetchall()
    )
    if len(cached) == 1:
        return ColorMatch(
            source_identifier=color_id,
            target_identifier=int(cached[0][0]),
            status=MappingStatus.RESOLVED,
            source=MappingSource.API_EXTERNAL_ID,
            confidence=1.0,
            candidates=(),
            explanation="API-confirmed external identifier",
            snapshot_id=snapshot,
        )
    rows = override or cached
    candidates = tuple(
        MatchCandidate(
            identifier=str(row[0]),
            name=None,
            reason="confirmed LDraw color identifier",
            confidence=1.0,
        )
        for row in rows
    )
    return ColorMatch(
        source_identifier=color_id,
        target_identifier=None,
        status=MappingStatus.AMBIGUOUS if candidates else MappingStatus.UNRESOLVED,
        source=MappingSource.USER_OVERRIDE
        if override
        else MappingSource.API_EXTERNAL_ID
        if cached
        else MappingSource.NONE,
        confidence=0.0,
        candidates=candidates,
        explanation="multiple confirmed color identifiers"
        if candidates
        else "no confirmed reverse color mapping",
        snapshot_id=snapshot,
    )
