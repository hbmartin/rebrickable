"""Generate the private operation registry from the vendored Swagger document."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
EXPECTED_OPERATIONS = 63
COMPATIBILITY_OVERLAY = {
    "lego_parts_list": {"inc_part_details": "boolean"},
    "lego_parts_read": {"inc_part_details": "boolean"},
    "lego_sets_parts_list": {
        "inc_part_details": "boolean",
        "inc_color_details": "boolean",
        "inc_minifig_parts": "boolean",
    },
    "users_allparts_list": {"inc_part_details": "boolean"},
    "users_parts_list": {"inc_part_details": "boolean"},
    "users_partlists_parts_list": {
        "inc_part_details": "boolean",
        "inc_color_details": "boolean",
    },
}


def operations(document: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            parameters = [
                *path_item.get("parameters", []),
                *operation.get("parameters", []),
            ]
            item = {
                "operation_id": operation["operationId"],
                "method": method.upper(),
                "path": path.removeprefix("/api/v3"),
                "parameters": parameters,
                "encoding": (operation.get("consumes") or [None])[0],
            }
            result.append(item)
    return result


def registry_source(items: list[dict[str, Any]], checksum: str) -> str:
    lines = [
        '"""Generated from the vendored Rebrickable Swagger document."""',
        "",
        "from dataclasses import dataclass",
        "from typing import Any",
        "",
        f'OPENAPI_SHA256 = "{checksum}"',
        "",
        "@dataclass(frozen=True, slots=True)",
        "class Parameter:",
        "    name: str",
        "    location: str",
        "    schema_type: str",
        "    required: bool",
        "    default: Any | None",
        "    description: str",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class Operation:",
        "    method: str",
        "    path: str",
        "    path_parameters: tuple[str, ...]",
        "    query_parameters: tuple[str, ...]",
        "    form_parameters: tuple[str, ...]",
        "    required_parameters: tuple[str, ...]",
        "    encoding: str | None",
        "    parameter_details: tuple[Parameter, ...]",
        "",
        "OPERATIONS: dict[str, Operation] = {",
    ]
    for operation in items:
        groups = {
            where: tuple(
                parameter["name"]
                for parameter in operation["parameters"]
                if parameter["in"] == where
            )
            for where in ("path", "query", "formData")
        }
        overlay = tuple(COMPATIBILITY_OVERLAY.get(operation["operation_id"], {}))
        required = tuple(
            parameter["name"]
            for parameter in operation["parameters"]
            if parameter.get("required")
        )
        details = tuple(
            (
                parameter["name"],
                parameter["in"],
                parameter.get("type", "string"),
                bool(parameter.get("required")),
                parameter.get("default"),
                parameter.get("description", ""),
            )
            for parameter in operation["parameters"]
        ) + tuple(
            (name, "query", schema_type, False, None, "Compatibility parameter")
            for name, schema_type in COMPATIBILITY_OVERLAY.get(
                operation["operation_id"], {}
            ).items()
        )
        rendered_details = ", ".join(
            f"Parameter({name!r}, {location!r}, {schema_type!r}, {required!r}, "
            f"{default!r}, {description!r})"
            for name, location, schema_type, required, default, description in details
        )
        rendered_details = f"({rendered_details},)" if rendered_details else "()"
        lines.append(
            f"    {operation['operation_id']!r}: Operation({operation['method']!r}, {operation['path']!r}, "
            f"{groups['path']!r}, {(groups['query'] + overlay)!r}, {groups['formData']!r}, {required!r}, "
            f"{operation['encoding']!r}, {rendered_details}),",
        )
    lines.extend(("}", ""))
    return "\n".join(lines)


def _class_name(operation_id: str) -> str:
    return "".join(part.title() for part in operation_id.split("_")) + "Query"


def query_types_source(items: list[dict[str, Any]], checksum: str) -> str:
    type_map = {
        "boolean": "bool",
        "integer": "int",
        "number": "int | float",
        "string": "str",
    }
    lines = [
        '"""Generated query keyword contracts for the Rebrickable API."""',
        "",
        "from typing import TypedDict",
        "",
        f'OPENAPI_SHA256 = "{checksum}"',
        "",
    ]
    for operation in items:
        path_parameters = {
            parameter["name"]
            for parameter in operation["parameters"]
            if parameter["in"] == "path"
        }
        query_parameters = {
            parameter["name"]: parameter.get("type", "string")
            for parameter in operation["parameters"]
            if parameter["in"] == "query" and parameter["name"] not in path_parameters
        }
        query_parameters.update(
            COMPATIBILITY_OVERLAY.get(operation["operation_id"], {})
        )
        has_query_parameters = any(
            parameter["in"] == "query" for parameter in operation["parameters"]
        ) or bool(COMPATIBILITY_OVERLAY.get(operation["operation_id"]))
        if not has_query_parameters:
            continue
        lines.append(
            f"class {_class_name(operation['operation_id'])}(TypedDict, total=False):"
        )
        if query_parameters:
            for name, schema_type in query_parameters.items():
                lines.append(f"    {name}: {type_map.get(schema_type, 'str')}")
        else:
            lines.append("    pass")
        lines.append("")
    return "\n".join(lines)


def expected_sha256(registry: Path) -> str | None:
    """Read the checksum the current registry was generated from."""
    if not registry.is_file():
        return None
    match = re.search(
        r'OPENAPI_SHA256 = "([0-9a-f]{64})"', registry.read_text(encoding="utf-8")
    )
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--query-types", type=Path)
    parser.add_argument("--allow-new-checksum", action="store_true")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    expected = expected_sha256(args.registry)
    if checksum != expected and not args.allow_new_checksum:
        raise SystemExit(f"unexpected OpenAPI checksum: {checksum}")
    document = json.loads(raw)
    items = operations(document)
    if len(items) != EXPECTED_OPERATIONS:
        raise SystemExit(
            f"expected {EXPECTED_OPERATIONS} operations, found {len(items)}"
        )
    args.registry.write_text(registry_source(items, checksum), encoding="utf-8")
    if args.query_types is not None:
        args.query_types.write_text(
            query_types_source(items, checksum), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
