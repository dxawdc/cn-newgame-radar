import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import (
    SCHEMA_VERSION,
    begin_immediate_with_retry,
    connect,
    connect_readonly,
    migrate_database,
    upsert_items,
)


class SourceItemUpsertTest(unittest.TestCase):
    def test_request_connection_skips_schema_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            self.assertEqual(migrate_database(path), SCHEMA_VERSION)
            with patch("newgame_monitor.db._apply_schema_migrations") as migration:
                conn = connect(path, migrate=False)
                try:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                finally:
                    conn.close()
            migration.assert_not_called()

    def test_wal_busy_timeout_and_readonly_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            finally:
                conn.close()
            readonly = connect_readonly(path)
            try:
                self.assertEqual(readonly.execute("SELECT COUNT(*) FROM source_items").fetchone()[0], 0)
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute("DELETE FROM source_items")
            finally:
                readonly.close()

    def test_wal_reader_remains_available_during_write_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            writer = connect(path)
            reader = connect_readonly(path)
            try:
                begin_immediate_with_retry(writer)
                writer.execute(
                    "INSERT INTO collection_runs(source,started_at,status) VALUES ('taptap','now','running')"
                )
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 0)
                writer.commit()
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 1)
            finally:
                reader.close()
                writer.close()

    def test_write_lock_retry_is_bounded(self):
        class BusyConnection:
            calls = 0

            def execute(self, _sql):
                self.calls += 1
                raise sqlite3.OperationalError("database is locked")

        conn = BusyConnection()
        with patch("newgame_monitor.db.time.sleep") as sleep:
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                begin_immediate_with_retry(conn, attempts=3, initial_delay=0.01)
        self.assertEqual(conn.calls, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02])

    def test_connect_creates_canonical_game_id_redirects(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(canonical_game_id_redirects)"
                )
            }
            self.assertEqual(
                columns,
                {"old_game_id", "new_game_id", "reason", "created_at"},
            )
            conn.execute(
                """
                INSERT INTO canonical_game_id_redirects(
                    old_game_id,new_game_id,reason,created_at
                ) VALUES (101,202,'normalized_name_merge','2026-08-27T10:00:00+08:00')
                """
            )
            row = conn.execute(
                "SELECT new_game_id FROM canonical_game_id_redirects WHERE old_game_id=101"
            ).fetchone()
            self.assertEqual(row["new_game_id"], 202)
            conn.close()

    def test_upsert_assigns_normalized_name_key_to_every_product(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            upsert_items(conn, [{
                "source": "oppo_gamecenter",
                "source_item_id": "game-normalized",
                "name": "头号禁区（限时开测）",
                "event_type": "beta",
                "event_time": "2026-08-27",
            }], "2026-08-27T08:00:00+08:00")
            row = conn.execute("SELECT canonical_key FROM source_items").fetchone()
            self.assertEqual(row["canonical_key"], "name:头号禁区")
            conn.close()

    def test_catalog_rebuild_refreshes_key_when_source_name_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            common = {
                "source": "oppo_gamecenter", "source_item_id": "renamed-game",
                "event_type": "launch", "event_time": "2026-08-28",
            }
            upsert_items(conn, [{
                **common, "name": "产品旧名",
            }], "2026-08-27T08:00:00+08:00")
            upsert_items(conn, [{
                **common, "name": "产品新名",
            }], "2026-08-27T09:00:00+08:00")
            rebuild_catalog(conn)
            row = conn.execute("SELECT name,canonical_key FROM source_items").fetchone()
            self.assertEqual(row["name"], "产品新名")
            self.assertEqual(row["canonical_key"], "name:产品新名")
            conn.close()

    def test_light_refresh_keeps_richer_detail_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            base = {
                "source": "oppo_gamecenter",
                "source_item_id": "game-1",
                "name": "样例游戏",
                "event_type": "launch",
                "event_time": "2026-08-20",
            }
            upsert_items(conn, [{
                **base,
                "developer": "样例开发公司",
                "gameplay_intro": "已核验的玩法摘要",
                "full_description": "完整详情" * 80,
                "raw": {"ui_detail": {"source": "oppo_gamecenter_app"}},
            }], "2026-08-20T08:00:00+08:00")
            upsert_items(conn, [{
                **base,
                "status": "首发已开启",
                "raw": {"calendar": "today"},
            }], "2026-08-21T08:00:00+08:00")

            row = conn.execute("SELECT * FROM source_items").fetchone()
            self.assertEqual(row["developer"], "样例开发公司")
            self.assertTrue(row["full_description"].startswith("完整详情"))
            raw = json.loads(row["raw_json"])
            self.assertIn("ui_detail", raw)
            self.assertEqual(raw["calendar"], "today")
            conn.close()

    def test_blank_event_end_time_does_not_erase_known_end_time(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            base = {
                "source": "oppo_gamecenter",
                "source_item_id": "beta-window",
                "name": "测试窗口样例",
                "event_type": "beta",
                "event_time": "2026-08-28",
            }
            upsert_items(conn, [{**base, "event_end_time": "2026-09-02"}], "2026-08-28T08:00:00+08:00")
            upsert_items(conn, [{**base, "event_end_time": ""}], "2026-08-28T09:00:00+08:00")
            row = conn.execute("SELECT event_end_time FROM source_items").fetchone()
            self.assertEqual(row["event_end_time"], "2026-09-02")
            conn.close()


if __name__ == "__main__":
    unittest.main()
