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
    from rebrickable.session import RebrickableSession

__all__ = [
    "normalize_ldraw_code",
    "resolve_ldraw_color",
    "resolve_ldraw_part",
    "resolve_rebrickable_color",
    "resolve_rebrickable_part",
]


async def resolve_ldraw_part(session: RebrickableSession, ldraw_code: str) -> PartMatch:
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
            ldraw_code,
            override[0],
            MappingStatus.RESOLVED,
            MappingSource.USER_OVERRIDE,
            1.0,
            (),
            "explicit user override",
            snapshot,
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
            ldraw_code,
            cached[0][0],
            MappingStatus.RESOLVED,
            MappingSource.API_EXTERNAL_ID,
            1.0,
            (),
            "API-confirmed external identifier",
            snapshot,
        )
    if cached:
        return PartMatch(
            ldraw_code,
            None,
            MappingStatus.AMBIGUOUS,
            MappingSource.API_EXTERNAL_ID,
            0.0,
            tuple(
                MatchCandidate(
                    str(row[0]), None, "API-confirmed external identifier", 1.0
                )
                for row in cached
            ),
            "multiple API-confirmed canonical identifiers",
            snapshot,
        )
    exact = await (
        await connection.execute(
            "SELECT part_num, name FROM parts WHERE part_num=? COLLATE NOCASE", (code,)
        )
    ).fetchone()
    if exact:
        return PartMatch(
            ldraw_code,
            exact[0],
            MappingStatus.RESOLVED,
            MappingSource.EXACT_CANONICAL_ID,
            0.98,
            (),
            "normalized identifiers are equal",
            snapshot,
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
        MatchCandidate(row[0], row[1], f"relationship {row[2]}", 0.6)
        for row in relationships
    )
    status = MappingStatus.AMBIGUOUS if candidates else MappingStatus.UNRESOLVED
    return PartMatch(
        ldraw_code,
        None,
        status,
        MappingSource.RELATIONSHIP_CANDIDATE if candidates else MappingSource.NONE,
        0.0,
        candidates,
        "relationship candidates require review"
        if candidates
        else "no offline mapping",
        snapshot,
    )


async def resolve_rebrickable_part(
    session: RebrickableSession, part_num: str
) -> PartMatch:
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
            part_num,
            override[0][0],
            MappingStatus.RESOLVED,
            MappingSource.USER_OVERRIDE,
            1.0,
            (),
            "explicit user override",
            snapshot,
        )
    if override:
        return PartMatch(
            part_num,
            None,
            MappingStatus.AMBIGUOUS,
            MappingSource.USER_OVERRIDE,
            0.0,
            tuple(
                MatchCandidate(str(row[0]), None, "user override", 1.0)
                for row in override
            ),
            "multiple user overrides map to this part",
            snapshot,
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
            part_num,
            cached[0][0],
            MappingStatus.RESOLVED,
            MappingSource.API_EXTERNAL_ID,
            1.0,
            (),
            "API-confirmed external identifier",
            snapshot,
        )
    if cached:
        return PartMatch(
            part_num,
            None,
            MappingStatus.AMBIGUOUS,
            MappingSource.API_EXTERNAL_ID,
            0.0,
            tuple(
                MatchCandidate(
                    str(row[0]), None, "API-confirmed external identifier", 1.0
                )
                for row in cached
            ),
            "multiple API-confirmed external identifiers",
            snapshot,
        )
    exact = await (
        await connection.execute(
            "SELECT part_num, name FROM parts WHERE part_num=?", (part_num,)
        )
    ).fetchone()
    if exact:
        return PartMatch(
            part_num,
            None,
            MappingStatus.AMBIGUOUS,
            MappingSource.EXACT_CANONICAL_ID,
            0.0,
            (
                MatchCandidate(
                    normalize_ldraw_code(part_num),
                    exact[1],
                    "same-identifier heuristic; LDraw existence unverified",
                    0.5,
                ),
            ),
            "identifier exists in Rebrickable; confirm with enrich_part_mapping"
            " or an override",
            snapshot,
        )
    return PartMatch(
        part_num,
        None,
        MappingStatus.UNRESOLVED,
        MappingSource.NONE,
        0.0,
        (),
        "no offline mapping",
        snapshot,
    )


async def resolve_ldraw_color(
    session: RebrickableSession, color: LDrawColorInfo | int
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
            code,
            int(override[0]),
            MappingStatus.RESOLVED,
            MappingSource.USER_OVERRIDE,
            1.0,
            (),
            "explicit user override",
            snapshot,
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
            code,
            int(cached[0][0]),
            MappingStatus.RESOLVED,
            MappingSource.API_EXTERNAL_ID,
            1.0,
            (),
            "API-confirmed external identifier",
            snapshot,
        )
    if cached:
        return ColorMatch(
            code,
            None,
            MappingStatus.AMBIGUOUS,
            MappingSource.API_EXTERNAL_ID,
            0.0,
            tuple(
                MatchCandidate(
                    str(row[0]), None, "API-confirmed external identifier", 1.0
                )
                for row in cached
            ),
            "multiple API-confirmed canonical identifiers",
            snapshot,
        )
    if isinstance(color, int):
        return ColorMatch(
            code,
            None,
            MappingStatus.UNRESOLVED,
            MappingSource.NONE,
            0.0,
            (),
            "color metadata is required for offline matching",
            snapshot,
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
            code,
            int(rgb_rows[0][0]),
            MappingStatus.RESOLVED,
            MappingSource.RGB,
            0.9,
            (),
            "unique RGB and transparency match",
            snapshot,
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
            code,
            int(name_rows[0][0]),
            MappingStatus.RESOLVED,
            MappingSource.NAME,
            0.8,
            (),
            "unique normalized name and transparency match",
            snapshot,
        )
    rows = rgb_rows or name_rows
    candidates = tuple(
        MatchCandidate(str(row[0]), row[1], "color candidate", 0.6) for row in rows
    )
    return ColorMatch(
        code,
        None,
        MappingStatus.AMBIGUOUS if candidates else MappingStatus.UNRESOLVED,
        MappingSource.RGB
        if rgb_rows
        else MappingSource.NAME
        if name_rows
        else MappingSource.NONE,
        0.0,
        candidates,
        "multiple color candidates" if candidates else "no offline color mapping",
        snapshot,
    )


async def resolve_rebrickable_color(
    session: RebrickableSession, color_id: int
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
            color_id,
            int(override[0][0]),
            MappingStatus.RESOLVED,
            MappingSource.USER_OVERRIDE,
            1.0,
            (),
            "explicit user override",
            snapshot,
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
            color_id,
            int(cached[0][0]),
            MappingStatus.RESOLVED,
            MappingSource.API_EXTERNAL_ID,
            1.0,
            (),
            "API-confirmed external identifier",
            snapshot,
        )
    rows = override or cached
    candidates = tuple(
        MatchCandidate(str(row[0]), None, "confirmed LDraw color identifier", 1.0)
        for row in rows
    )
    return ColorMatch(
        color_id,
        None,
        MappingStatus.AMBIGUOUS if candidates else MappingStatus.UNRESOLVED,
        MappingSource.USER_OVERRIDE
        if override
        else MappingSource.API_EXTERNAL_ID
        if cached
        else MappingSource.NONE,
        0.0,
        candidates,
        "multiple confirmed color identifiers"
        if candidates
        else "no confirmed reverse color mapping",
        snapshot,
    )
