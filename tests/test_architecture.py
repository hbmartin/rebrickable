from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from rebrickable import Bom, Config, RebrickableSession, SearchKind
from rebrickable.catalog.database import CatalogPaths

ROOT = Path(__file__).resolve().parents[1]


def runtime_session_imports(source: str) -> list[int]:
    violations: list[int] = []

    def walk(nodes: list[ast.AST], *, type_only: bool = False) -> None:
        for node in nodes:
            if isinstance(node, ast.If):
                test = node.test
                guarded = (
                    isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
                ) or (
                    isinstance(test, ast.Attribute)
                    and isinstance(test.value, ast.Name)
                    and test.value.id == "typing"
                    and test.attr == "TYPE_CHECKING"
                )
                walk(node.body, type_only=type_only or guarded)
                walk(node.orelse, type_only=type_only)
            elif isinstance(node, ast.ImportFrom) and not type_only:
                if (
                    node.module == "rebrickable.session"
                    or (
                        node.module == "rebrickable"
                        and any(alias.name == "session" for alias in node.names)
                    )
                    or (
                        node.level > 0
                        and (
                            node.module == "session"
                            or (
                                node.module is None
                                and any(alias.name == "session" for alias in node.names)
                            )
                        )
                    )
                ):
                    violations.append(node.lineno)
            elif isinstance(node, ast.Import) and not type_only:
                if any(alias.name == "rebrickable.session" for alias in node.names):
                    violations.append(node.lineno)
            else:
                walk(list(ast.iter_child_nodes(node)), type_only=type_only)

    walk(ast.parse(source).body)
    return violations


@pytest.mark.parametrize("directory", ["catalog", "bridge"])
def test_leaf_modules_have_no_reverse_runtime_imports(directory: str) -> None:
    for path in (ROOT / "src/rebrickable" / directory).rglob("*.py"):
        assert not runtime_session_imports(path.read_text()), path.name


@pytest.mark.parametrize(
    ("source", "allowed"),
    [
        ("from rebrickable.session import RebrickableSession", False),
        ("import rebrickable.session as s", False),
        ("from ..session import RebrickableSession", False),
        ("from rebrickable import session", False),
        ("from .. import session", False),
        ("try:\n pass\nexcept Exception:\n import rebrickable.session", False),
        ("def repository():\n import rebrickable.session", False),
        (
            "if TYPE_CHECKING:\n from rebrickable.session import RebrickableSession",
            True,
        ),
        ("if typing.TYPE_CHECKING:\n import rebrickable.session", True),
        ("if TYPE_CHECKING:\n pass\nelse:\n import rebrickable.session", False),
    ],
)
def test_architecture_guard(source: str, *, allowed: bool) -> None:
    assert bool(runtime_session_imports(source)) is not allowed


async def test_offline_operations_leave_database_immutable(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("offline operation constructed an HTTP client")

    monkeypatch.setattr("httpx2.AsyncClient", forbidden)
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_text()
    database = paths.database_for(json.loads(pointer)["snapshot_id"])
    before = database.read_bytes()
    async with await RebrickableSession.connect(catalog_config) as session:
        assert await session.parts.get("3001")
        assert (await session.search("brick", kinds={SearchKind.PART})).hits
        assert (await session.sets.bill_of_materials("100-1")).rows
        bom = Bom.from_rebrickable_csv("part_num,color_id,quantity\n3001,4,2\n")
        assert (await session.validate_bom(bom)).exact_count == 1
        assert (
            await session.ldraw.resolve_ldraw_part("3001")
        ).target_identifier == "3001"
    assert paths.active_pointer.read_text() == pointer
    assert database.read_bytes() == before
