# API v3 reference

`RebrickableClient` exposes an explicit async method for each of the 63 operations
in the vendored 2026-08-01 Swagger 2.0 document. The SHA-256 is
`91b49e310f8fb2db4ff7474e2775921897e10319a71ec053cac61f3a40fa7cb6`.
`rebrickable api-spec` reads this artifact without an API key.

Every request authenticates only with `Authorization: key …`. Path identifiers
are positional; optional parameters are keyword-only. Call-level user tokens
override the client token. List methods return `ApiPage[T]`; every list operation
has an `iter_*` companion that requests 1,000 rows, validates HTTPS and origin,
and detects loops.

The client paces at one request per second by default. Idempotent calls retry
connection failures and selected 5xx responses up to three times. A 429 honors
`Retry-After`, then safe response detail, then bounded jittered backoff; a
server delay beyond `Config.max_retry_after` (300 s default) raises
`ApiThrottledError` immediately instead of blocking. Mutations are not replayed
unless their operation explicitly opts into a mutation retry policy.
`sync_user_sets` additionally requires `confirm_replace=True`. Sequence inputs
to `add_user_sets`, `add_user_part_list_parts`, and `add_user_set_list_sets`
POST one JSON array body, matching the upstream bulk contracts. Their
`MutationResult` separates requested, accepted, skipped, and unaccepted records
and exposes counts for each. Explicit `*_sequential` helpers retain the older
stop-on-first-failure workflow and raise `BatchMutationError` with the accepted
prefix and failing index.

Transport DTOs are frozen Pydantic v2 models. Additive response fields are
recursively frozen in `extra`; decoding deliberately raises `ApiDecodeError` for
missing or malformed required fields. DTOs never escape into the local domain
layer implicitly.

Specialized mutation records are exported from `rebrickable` and remain
available through `rebrickable.api.models`. Every public query method uses a
generated operation-specific `TypedDict`, so static checkers reject misspelled
keywords and wrong primitive types. The generated operation registry retains
each parameter's location, type, required flag, default, and description;
keyword contracts live in `rebrickable.api.query_types`. The compatibility
overlay retains guide-only parameters missing from Swagger, including
`inc_part_details`, `inc_color_details`, and `inc_minifig_parts`.
