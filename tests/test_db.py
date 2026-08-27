import json
import tempfile
import unittest
from pathlib import Path

from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import connect, upsert_items


class SourceItemUpsertTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
