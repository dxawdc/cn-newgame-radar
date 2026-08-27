import json
import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    name TEXT NOT NULL,
    package_name TEXT,
    developer TEXT,
    category TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    gameplay_intro TEXT,
    full_description TEXT,
    icon_url TEXT,
    detail_url TEXT,
    rating REAL,
    version_name TEXT,
    size_bytes INTEGER,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL DEFAULT '',
    event_end_time TEXT NOT NULL DEFAULT '',
    canonical_key TEXT,
    status TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(source, source_item_id, event_type, event_time)
);

CREATE INDEX IF NOT EXISTS idx_source_items_event_time ON source_items(event_time);
CREATE INDEX IF NOT EXISTS idx_source_items_name ON source_items(name);
CREATE TABLE IF NOT EXISTS canonical_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    developer TEXT,
    category TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    gameplay_intro TEXT,
    icon_url TEXT,
    rating REAL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS canonical_members (
    game_id INTEGER NOT NULL,
    source_row_id INTEGER NOT NULL,
    PRIMARY KEY (game_id, source_row_id),
    FOREIGN KEY (game_id) REFERENCES canonical_games(id) ON DELETE CASCADE,
    FOREIGN KEY (source_row_id) REFERENCES source_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_canonical_members_source ON canonical_members(source_row_id);
CREATE INDEX IF NOT EXISTS idx_canonical_games_name ON canonical_games(name);

CREATE TABLE IF NOT EXISTS canonical_key_redirects (
    old_key TEXT PRIMARY KEY,
    new_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canonical_key_redirects_new
ON canonical_key_redirects(new_key);

CREATE TABLE IF NOT EXISTS canonical_game_id_redirects (
    old_game_id INTEGER PRIMARY KEY,
    new_game_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (old_game_id > 0),
    CHECK (new_game_id > 0),
    CHECK (old_game_id <> new_game_id)
);

CREATE INDEX IF NOT EXISTS idx_canonical_game_id_redirects_new
ON canonical_game_id_redirects(new_game_id);

CREATE TABLE IF NOT EXISTS icon_assets (
    source_url TEXT PRIMARY KEY,
    relative_path TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    content_type TEXT,
    byte_size INTEGER,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_icon_assets_status ON icon_assets(status);

CREATE TABLE IF NOT EXISTS screenshot_assets (
    source_url TEXT PRIMARY KEY,
    relative_path TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    content_type TEXT,
    byte_size INTEGER,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_screenshot_assets_status ON screenshot_assets(status);

CREATE TABLE IF NOT EXISTS enrichment_lookup_cache (
    provider TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    query_name TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (provider, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_enrichment_lookup_checked
ON enrichment_lookup_cache(provider, checked_at);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    normalized_username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('superadmin', 'admin', 'user')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by INTEGER,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role, is_active);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry ON user_sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    attempt_key TEXT PRIMARY KEY,
    failed_count INTEGER NOT NULL,
    window_started_at TEXT NOT NULL,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS user_favorites (
    user_id INTEGER NOT NULL,
    game_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_followed_at TEXT NOT NULL,
    PRIMARY KEY (user_id, game_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_favorites_game ON user_favorites(game_key);

CREATE TABLE IF NOT EXISTS favorite_activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    game_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('follow', 'unfollow')),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_favorite_activity_user_time
ON favorite_activity_logs(user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_favorite_activity_game
ON favorite_activity_logs(game_key, occurred_at DESC);

CREATE TABLE IF NOT EXISTS user_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_api_keys_user
ON user_api_keys(user_id, revoked_at, created_at DESC);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # 对已经存在的第一版数据库做无损迁移。
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_items)")}
    if "event_end_time" not in columns:
        conn.execute("ALTER TABLE source_items ADD COLUMN event_end_time TEXT NOT NULL DEFAULT ''")
    if "canonical_key" not in columns:
        conn.execute("ALTER TABLE source_items ADD COLUMN canonical_key TEXT")
    if "full_description" not in columns:
        conn.execute("ALTER TABLE source_items ADD COLUMN full_description TEXT")
    favorite_columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_favorites)")}
    if "last_followed_at" not in favorite_columns:
        conn.execute("ALTER TABLE user_favorites ADD COLUMN last_followed_at TEXT")
        conn.execute("UPDATE user_favorites SET last_followed_at=created_at WHERE last_followed_at IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_items_canonical_key ON source_items(canonical_key)")
    conn.commit()
    return conn


def upsert_items(conn: sqlite3.Connection, items: list[dict], observed_at: str) -> int:
    # 每条来源记录在写入时先生成保守的名称标准键；全量结构化变体归并由
    # rebuild_catalog 在掌握所有产品名称后统一完成。
    from .catalog import canonical_key_for

    sql = """
    INSERT INTO source_items (
        source, source_item_id, name, package_name, developer, category,
        tags_json, gameplay_intro, full_description, icon_url, detail_url, rating, version_name,
        size_bytes, event_type, event_time, event_end_time, canonical_key,
        status, first_seen_at, last_seen_at, raw_json
    ) VALUES (
        :source, :source_item_id, :name, :package_name, :developer, :category,
        :tags_json, :gameplay_intro, :full_description, :icon_url, :detail_url, :rating, :version_name,
        :size_bytes, :event_type, :event_time, :event_end_time, :canonical_key,
        :status, :first_seen_at, :last_seen_at, :raw_json
    )
    ON CONFLICT(source, source_item_id, event_type, event_time) DO UPDATE SET
        name=CASE
            WHEN NULLIF(json_extract(source_items.raw_json, '$.official_public_detail.name'), '') IS NOT NULL
            THEN source_items.name ELSE excluded.name END,
        package_name=COALESCE(NULLIF(excluded.package_name, ''), source_items.package_name),
        developer=COALESCE(NULLIF(excluded.developer, ''), source_items.developer),
        category=COALESCE(NULLIF(excluded.category, ''), source_items.category),
        tags_json=CASE WHEN excluded.tags_json='[]' THEN source_items.tags_json ELSE excluded.tags_json END,
        gameplay_intro=CASE
            WHEN length(trim(COALESCE(excluded.gameplay_intro, ''))) >= length(trim(COALESCE(source_items.gameplay_intro, '')))
            THEN excluded.gameplay_intro ELSE source_items.gameplay_intro END,
        full_description=CASE
            WHEN length(trim(COALESCE(excluded.full_description, ''))) >= length(trim(COALESCE(source_items.full_description, '')))
            THEN excluded.full_description ELSE source_items.full_description END,
        icon_url=COALESCE(NULLIF(excluded.icon_url, ''), source_items.icon_url),
        detail_url=COALESCE(NULLIF(excluded.detail_url, ''), source_items.detail_url),
        rating=COALESCE(excluded.rating, source_items.rating),
        version_name=COALESCE(NULLIF(excluded.version_name, ''), source_items.version_name),
        size_bytes=COALESCE(excluded.size_bytes, source_items.size_bytes),
        status=COALESCE(NULLIF(excluded.status, ''), source_items.status),
        event_end_time=excluded.event_end_time,
        last_seen_at=excluded.last_seen_at,
        raw_json=json_patch(source_items.raw_json, excluded.raw_json)
    """
    for item in items:
        row = {
            "package_name": None, "developer": None, "category": None,
            "tags_json": "[]", "gameplay_intro": None, "full_description": None, "icon_url": None,
            "detail_url": None, "rating": None, "version_name": None,
            "size_bytes": None, "event_time": "", "event_end_time": "",
            "canonical_key": None, "status": None,
            **item,
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
        }
        row["canonical_key"] = canonical_key_for(row["name"], row["package_name"])
        row["tags_json"] = json.dumps(row.pop("tags", []), ensure_ascii=False)
        row["raw_json"] = json.dumps(row.pop("raw", {}), ensure_ascii=False, separators=(",", ":"))
        conn.execute(sql, row)
    conn.commit()
    return len(items)
