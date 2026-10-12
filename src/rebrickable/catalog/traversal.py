"""Depth-bounded, cycle-safe hierarchy traversal SQL shared by catalog queries.

Each CTE binds one parameter (the seed theme id) and tracks the recursion
depth plus a `/id/`-delimited path of visited ids: expansion stops at
`MAX_TRAVERSAL_DEPTH` levels and never revisits an id already on the path,
so corrupt parent links that form cycles terminate without duplicate rows.
"""

from __future__ import annotations

from typing import Final

MAX_TRAVERSAL_DEPTH: Final = 100

# Imported by catalog query and search modules.
# codeql[py/unused-global-variable]
THEME_SUBTREE_CTE: Final = (
    "theme_tree(id, depth, path) AS ("  # noqa: S608 - internal SQL clauses/identifiers; values are bound separately
    "SELECT id, 0, printf('/%d/', id) FROM themes WHERE id=? UNION ALL "
    "SELECT t.id, tt.depth + 1, tt.path || t.id || '/' "
    "FROM themes t JOIN theme_tree tt ON t.parent_id=tt.id "
    f"WHERE tt.depth < {MAX_TRAVERSAL_DEPTH} "
    "AND instr(tt.path, printf('/%d/', t.id)) = 0)"
)

# Imported by catalog query and search modules.
# codeql[py/unused-global-variable]
THEME_LINEAGE_CTE: Final = (
    "theme_lineage(id, parent_id, depth, path) AS ("  # noqa: S608 - internal SQL clauses/identifiers; values are bound separately
    "SELECT id, parent_id, 0, printf('/%d/', id) FROM themes WHERE id=? UNION ALL "
    "SELECT t.id, t.parent_id, tl.depth + 1, tl.path || t.id || '/' "
    "FROM themes t JOIN theme_lineage tl ON t.id=tl.parent_id "
    f"WHERE tl.depth < {MAX_TRAVERSAL_DEPTH} "
    "AND instr(tl.path, printf('/%d/', t.id)) = 0)"
)
