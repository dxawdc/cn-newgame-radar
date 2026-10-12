"""定向详情读取的内容兼容性和读取范围回归测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from newgame_monitor import webapp
from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import connect
from newgame_monitor.db import upsert_items


class DetailReadTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.db = root / "detail.db"
        self.icons = root / "icons"
        self.screenshots = root / "screenshots"
        self.icons.mkdir()
        self.screenshots.mkdir()
        for name, value in (("DB_PATH", self.db), ("ICON_DIR", self.icons), ("SCREENSHOT_DIR", self.screenshots)):
            patcher = patch.object(webapp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        conn = connect(self.db)
        upsert_items(conn, [
            {"source": "taptap", "source_item_id": "target-tap", "name": "目标游戏",
             "event_type": "reservation", "event_time": "2026-10-13", "icon_url": "https://tapimg.com/icon.png",
             "full_description": "完整玩法介绍" * 20,
             "raw": {"detail_screenshots": ["https://tapimg.com/a.png", "https://tapimg.com/missing.png"]}},
            {"source": "taptap", "source_item_id": "target-tap", "name": "目标游戏",
             "event_type": "launch", "event_time": "2026-10-15", "raw": {}},
            {"source": "apple_appstore_cn", "source_item_id": "target-apple", "name": "目标游戏",
             "event_type": "launch", "event_time": "2026-10-15", "gameplay_intro": "另一个渠道的介绍",
             "raw": {"screenshot_urls": ["https://example.com/apple.png"]}},
            {"source": "taptap", "source_item_id": "other", "name": "无关游戏",
             "event_type": "launch", "event_time": "2026-10-15",
             "raw": {"unrelated_marker": "x" * 100000, "detail_screenshots": ["https://tapimg.com/unrelated.png"]}},
        ], "2026-10-12T10:00:00+08:00")
        rebuild_catalog(conn)
        self.target = dict(conn.execute("SELECT * FROM canonical_games WHERE name='目标游戏'").fetchone())
        conn.execute("INSERT INTO icon_assets(source_url,relative_path,status,updated_at) VALUES (?,?,?,?)",
                     ("https://tapimg.com/icon.png", "icon.webp", "success", "2026-10-12"))
        for url, relative in (("https://tapimg.com/a.png", "a.webp"),
                              ("https://tapimg.com/missing.png", "missing.webp"),
                              ("https://tapimg.com/unrelated.png", "unrelated.webp")):
            conn.execute("INSERT INTO screenshot_assets(source_url,relative_path,status,updated_at) VALUES (?,?,?,?)",
                         (url, relative, "success", "2026-10-12"))
        conn.commit()
        conn.close()
        (self.icons / "icon.webp").touch()
        (self.screenshots / "a.webp").touch()
        (self.screenshots / "unrelated.webp").touch()

    @staticmethod
    def request():
        return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})

    def test_scoped_payload_matches_catalog_with_cached_missing_and_remote_media(self):
        conn = webapp._read_conn()
        self.addCleanup(conn.close)
        original = next(game for game in webapp._load_catalog(conn) if game["id"] == self.target["id"])
        scoped = webapp._load_game(conn, game_uuid=self.target["game_uuid"])
        self.assertEqual(webapp._serialize(scoped), webapp._serialize(original))
        self.assertEqual(webapp._latest_game_intro(scoped), webapp._latest_game_intro(original))
        self.assertEqual(scoped["_local_icon_url"], f"{webapp.BASE_PATH}/icons/icon.webp")
        self.assertEqual(scoped["_screenshot_assets"], {"https://tapimg.com/a.png": "a.webp"})

    def test_detail_does_not_read_unrelated_raw_media_or_full_catalog(self):
        visited = []
        original_exists = Path.exists

        def record_exists(path):
            visited.append(path.name)
            return original_exists(path)

        original_extract = webapp.extract_gallery_urls

        def guarded_extract(source, raw):
            self.assertNotIn("unrelated_marker", raw)
            return original_extract(source, raw)

        with patch.object(webapp, "_load_catalog", side_effect=AssertionError("不应加载全库")), \
             patch.object(webapp, "extract_gallery_urls", side_effect=guarded_extract), \
             patch.object(Path, "exists", record_exists):
            for endpoint, key in ((webapp.game_detail_by_uuid, self.target["game_uuid"]),
                                  (webapp.game_detail, self.target["id"])):
                result = endpoint(key, self.request())
                self.assertEqual(result["name"], "目标游戏")
        self.assertNotIn("unrelated.webp", visited)

    def test_uuid_redirect_preserves_followed_state(self):
        conn = connect(self.db)
        conn.execute("INSERT INTO canonical_game_uuid_redirects(old_game_uuid,new_game_uuid,reason,created_at) VALUES (?,?,?,?)",
                     ("old-uuid", self.target["game_uuid"], "merge", "2026-10-12"))
        conn.commit()
        conn.close()
        with patch.object(webapp, "_session_context", return_value={"user": {"id": 123}}), \
             patch.object(webapp, "_favorite_dates", return_value={self.target["game_uuid"]: "2026-10-12T10:00:00+08:00"}):
            result = webapp.game_detail_by_uuid("old-uuid", self.request())
        self.assertEqual(result["uuid"], self.target["game_uuid"])
        self.assertTrue(result["followed"])
        self.assertEqual(result["last_followed_at"], "2026-10-12T10:00:00+08:00")

    def test_not_found_does_not_load_catalog_or_rebuild(self):
        with patch.object(webapp, "_load_catalog", side_effect=AssertionError("不应加载全库")), \
             patch.object(webapp, "rebuild_catalog", side_effect=AssertionError("不应重建目录")):
            self.assertEqual(webapp.game_detail_by_uuid("unknown", self.request()), {"error": "not_found"})
            self.assertEqual(webapp.game_detail(-1, self.request()), {"error": "not_found"})

    def test_empty_catalog_is_read_only(self):
        empty_db = Path(self.temporary.name) / "empty.db"
        connect(empty_db).close()
        with patch.object(webapp, "DB_PATH", empty_db), \
             patch.object(webapp, "rebuild_catalog", side_effect=AssertionError("不应重建目录")):
            self.assertEqual(webapp.game_detail_by_uuid("unknown", self.request()), {"error": "not_found"})
            self.assertEqual(webapp.game_detail(1, self.request()), {"error": "not_found"})

    def test_local_icon_and_local_screenshot_match_previous_behavior(self):
        conn = connect(self.db)
        conn.execute("UPDATE canonical_games SET icon_url=? WHERE id=?", ("local-icon://local.webp", self.target["id"]))
        conn.execute("UPDATE source_items SET raw_json=? WHERE name='目标游戏'",
                     ('{"detail_screenshots":["local-screenshot://local.webp"]}',))
        conn.commit()
        (self.icons / "local.webp").touch()
        original = next(game for game in webapp._load_catalog(conn) if game["id"] == self.target["id"])
        scoped = webapp._load_game(conn, game_id=self.target["id"])
        self.assertEqual(webapp._serialize(scoped), webapp._serialize(original))
        conn.close()


if __name__ == "__main__":
    unittest.main()
