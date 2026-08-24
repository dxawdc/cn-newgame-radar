import json
import tempfile
import unittest
from pathlib import Path

from newgame_monitor.db import connect, upsert_items


class SourceItemUpsertTest(unittest.TestCase):
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
