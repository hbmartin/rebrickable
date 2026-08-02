"""Versioned SQLite schema for imported catalog snapshots."""

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE snapshot_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE themes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER
);
CREATE TABLE colors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rgb TEXT NOT NULL,
    is_trans INTEGER NOT NULL CHECK(is_trans IN (0, 1)),
    num_parts INTEGER NOT NULL CHECK(num_parts >= 0),
    num_sets INTEGER NOT NULL CHECK(num_sets >= 0),
    year_from INTEGER,
    year_to INTEGER
);
CREATE TABLE part_categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE parts (
    part_num TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    part_cat_id INTEGER NOT NULL,
    material TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE part_relationships (
    rel_type TEXT NOT NULL,
    child_part_num TEXT NOT NULL,
    parent_part_num TEXT NOT NULL,
    PRIMARY KEY(rel_type, child_part_num, parent_part_num)
) WITHOUT ROWID;
CREATE TABLE elements (
    element_id TEXT PRIMARY KEY,
    part_num TEXT NOT NULL,
    color_id INTEGER NOT NULL,
    design_id TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE sets (
    set_num TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    year INTEGER NOT NULL,
    theme_id INTEGER NOT NULL,
    num_parts INTEGER NOT NULL CHECK(num_parts >= 0),
    image_url TEXT
) WITHOUT ROWID;
CREATE TABLE minifigs (
    fig_num TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    num_parts INTEGER NOT NULL CHECK(num_parts >= 0),
    image_url TEXT
) WITHOUT ROWID;
CREATE TABLE inventories (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 0),
    owner_num TEXT NOT NULL
);
CREATE TABLE inventory_parts (
    inventory_id INTEGER NOT NULL,
    part_num TEXT NOT NULL,
    color_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    is_spare INTEGER NOT NULL CHECK(is_spare IN (0, 1)),
    image_url TEXT
);
CREATE TABLE inventory_sets (
    inventory_id INTEGER NOT NULL,
    set_num TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0)
);
CREATE TABLE inventory_minifigs (
    inventory_id INTEGER NOT NULL,
    fig_num TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0)
);
CREATE TABLE api_crosswalk_cache (
    entity_kind TEXT NOT NULL,
    external_system TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    PRIMARY KEY(entity_kind, external_system, external_id, canonical_id)
) WITHOUT ROWID;
CREATE TABLE user_mapping_overrides (
    entity_kind TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_system TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(entity_kind, source_system, source_id, target_system)
) WITHOUT ROWID;
CREATE TABLE search_documents (
    rowid INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT,
    external_ids TEXT NOT NULL DEFAULT '',
    normalized_name TEXT NOT NULL,
    year INTEGER,
    theme_id INTEGER,
    num_parts INTEGER,
    category_id INTEGER,
    material TEXT
);
CREATE VIRTUAL TABLE search_fts USING fts5(
    canonical_id,
    title,
    subtitle,
    external_ids,
    content='search_documents',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIEW latest_inventories AS
SELECT i.id, i.version, i.owner_num
FROM inventories AS i
JOIN (
    SELECT owner_num, MAX(CAST(version AS INTEGER)) AS max_version
    FROM inventories
    GROUP BY owner_num
) AS latest
ON latest.owner_num = i.owner_num AND latest.max_version = CAST(i.version AS INTEGER);
CREATE INDEX idx_themes_parent ON themes(parent_id);
CREATE INDEX idx_parts_category ON parts(part_cat_id);
CREATE INDEX idx_relationships_child ON part_relationships(child_part_num);
CREATE INDEX idx_relationships_parent ON part_relationships(parent_part_num);
CREATE INDEX idx_elements_part_color ON elements(part_num, color_id);
CREATE INDEX idx_elements_design ON elements(design_id);
CREATE INDEX idx_sets_theme_year ON sets(theme_id, year);
CREATE INDEX idx_inventories_owner_version ON inventories(owner_num, version DESC);
CREATE INDEX idx_inventory_parts_inventory ON inventory_parts(inventory_id);
CREATE INDEX idx_inventory_parts_part_color ON inventory_parts(part_num, color_id);
CREATE INDEX idx_inventory_sets_inventory ON inventory_sets(inventory_id);
CREATE INDEX idx_inventory_sets_set ON inventory_sets(set_num);
CREATE INDEX idx_inventory_minifigs_inventory ON inventory_minifigs(inventory_id);
CREATE INDEX idx_inventory_minifigs_fig ON inventory_minifigs(fig_num);
CREATE INDEX idx_search_kind_id ON search_documents(kind, canonical_id);
CREATE INDEX idx_search_filters ON search_documents(kind, year, theme_id, num_parts, category_id, material);
"""
