# Keyword-only constructors (unreleased)

The garuff cleanup makes package dataclass constructors keyword-only. This
includes offline domain records, BOM and LDraw records, configuration and
progress values, namespace references, and `ApiError` and its standard
subclasses. This is a breaking change from released 1.1 and requires the next
major release; package version metadata is not changed by this unreleased work.

Pass every field by name:

```python
from rebrickable import ColorRef, LDrawBomItem, PartRef

part = PartRef(system="ldraw", value="3001.dat")
color = ColorRef(system="ldraw", value=4)
item = LDrawBomItem(part_num="3001.dat", color_code=4, quantity=2)
```

Previously, the corresponding calls were `PartRef("ldraw", "3001.dat")`,
`ColorRef("ldraw", 4)`, and `LDrawBomItem("3001.dat", 4, 2)`. Those calls now
raise `TypeError`. Dataclass constructors no longer expose positional
`__match_args__`; use keyword class patterns such as
`case LDrawBomItem(part_num=part, color_code=color, quantity=quantity)`.

The custom `PartRef` and `ColorRef` initializers also require named arguments.
Their existing namespace conversion and identifier normalization are preserved.
Values remain frozen and slotted, retain their existing equality and hash
behavior, and serialize with the same field names and values.

Construct structured API exceptions with named fields:

```python
from rebrickable import ApiError

error = ApiError(
    status=429,
    path_template="/lego/parts/",
    operation_id="lego_parts_list",
    detail="slow down",
    request_id="request-123",
    retry_after=2.0,
)
```

The error's existing `args` tuple and string representation are preserved.
Newly pickled errors preserve their exception subclass and field values when
restored. Legacy pickles that reconstruct an exception through positional
arguments cannot be loaded with the new constructor; recreate such saved
exceptions from their named fields before migrating.

API client and repository method signatures remain unchanged. Pydantic request
and response DTO construction is unchanged. Catalog snapshot schema version 1,
asynchronous operations, offline query behavior, and mutation guards are
unchanged.

The private generated `Parameter` and `Operation` records also use keyword-only
construction. Their generator and generated instances are updated together;
the vendored OpenAPI checksum, operation set, and query types are unchanged.
