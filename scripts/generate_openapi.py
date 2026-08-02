"""Generate private operation metadata and Pydantic parameter models."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

EXPECTED_SHA256 = "91b49e310f8fb2db4ff7474e2775921897e10319a71ec053cac61f3a40fa7cb6"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
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


def normalized_schema(items: list[dict[str, Any]]) -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    for operation in items:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in operation["parameters"]:
            schema: dict[str, Any] = {"type": parameter.get("type", "string")}
            if "items" in parameter:
                schema["items"] = parameter["items"]
            properties[parameter["name"]] = schema
            if parameter.get("required"):
                required.append(parameter["name"])
        for name, kind in COMPATIBILITY_OVERLAY.get(
            operation["operation_id"], {}
        ).items():
            properties[name] = {"type": kind}
        definition: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            definition["required"] = required
        definitions[operation["operation_id"] + "Parameters"] = definition
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": definitions,
    }


def registry_source(items: list[dict[str, Any]], checksum: str) -> str:
    lines = [
        '"""Generated from the vendored Rebrickable Swagger document."""',
        "",
        "from dataclasses import dataclass",
        "",
        f'OPENAPI_SHA256 = "{checksum}"',
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
        lines.append(
            f"    {operation['operation_id']!r}: Operation({operation['method']!r}, {operation['path']!r}, "
            f"{groups['path']!r}, {(groups['query'] + overlay)!r}, {groups['formData']!r}, {required!r}, {operation['encoding']!r}),",
        )
    lines.extend(("}", ""))
    return "\n".join(lines)


def redact_generated_secrets(path: Path) -> None:
    """Make private generated credential fields safe to repr deterministically."""
    source = path.read_text(encoding="utf-8")
    source = source.replace(
        "from pydantic import BaseModel, RootModel",
        "from pydantic import BaseModel, Field, RootModel",
    )
    lines: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        output_line = line
        if line.startswith("    ") and stripped.startswith(
            ("password:", "user_token:")
        ):
            declaration, separator, default = line.partition(" = ")
            output_line = (
                f"{declaration} = Field(default={default}, repr=False)"
                if separator
                else f"{line} = Field(repr=False)"
            )
        lines.append(output_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--allow-new-checksum", action="store_true")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != EXPECTED_SHA256 and not args.allow_new_checksum:
        raise SystemExit(f"unexpected OpenAPI checksum: {checksum}")
    document = json.loads(raw)
    items = operations(document)
    if len(items) != 63:
        raise SystemExit(f"expected 63 operations, found {len(items)}")
    args.registry.write_text(registry_source(items, checksum), encoding="utf-8")
    if args.models is not None:
        with tempfile.TemporaryDirectory() as temp:
            schema = Path(temp) / "parameters.json"
            schema.write_text(
                json.dumps(normalized_schema(items), sort_keys=True), encoding="utf-8"
            )
            subprocess.run(
                [
                    "datamodel-codegen",
                    "--input",
                    str(schema),
                    "--input-file-type",
                    "jsonschema",
                    "--output",
                    str(args.models),
                    "--output-model-type",
                    "pydantic_v2.BaseModel",
                    "--target-python-version",
                    "3.12",
                    "--disable-timestamp",
                ],
                check=True,
            )
            redact_generated_secrets(args.models)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
