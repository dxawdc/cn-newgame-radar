import tempfile
import unittest
from pathlib import Path

from newgame_monitor.catalog import (
    audit_catalog_completeness,
    clean_game_name,
    normalize_game_name,
    rebuild_catalog,
)
from newgame_monitor.db import connect, upsert_items


class CatalogTest(unittest.TestCase):
    def test_channel_and_event_suffixes_are_removed(self):
        self.assertEqual(clean_game_name("诡秘之主-8月21日上线"), "诡秘之主")
        self.assertEqual(clean_game_name("诡秘之主-预下载(官服)"), "诡秘之主")
        self.assertEqual(clean_game_name("一念长安（入长安开启回合新章）"), "一念长安")
        self.assertEqual(clean_game_name("夜幕之下-全员恶人群像(官服)"), "夜幕之下")
        self.assertEqual(clean_game_name("远光84（官服）"), "远光84")
        self.assertEqual(clean_game_name("代号：新生-动物搜打撤新游"), "代号:新生")
        self.assertEqual(clean_game_name("佣兵大冒险（肉鸽废土横扫尸潮）"), "佣兵大冒险")
        self.assertEqual(clean_game_name("客官里面请（删档测试）"), "客官里面请")
        self.assertEqual(clean_game_name("开天英雄(安装赢HUAWEI Mate80 Pro)"), "开天英雄")
        self.assertEqual(normalize_game_name("光 · 遇"), normalize_game_name("光·遇"))

    def test_cross_source_items_merge_and_keep_rich_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-19T10:00:00+08:00"
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "1", "name": "样例游戏",
                "developer": "样例工作室", "category": "策略", "tags": ["SLG"],
                "gameplay_intro": "一款策略游戏", "icon_url": "https://example.com/icon.png",
                "event_type": "launch", "event_time": "2026-08-20", "raw": {},
            }], observed)
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "2", "name": "样例游戏（官服）",
                "event_type": "reservation", "event_time": "2026-08-19", "raw": {},
            }], observed)
            self.assertEqual(rebuild_catalog(conn), 1)
            game = conn.execute("SELECT * FROM canonical_games").fetchone()
            self.assertEqual(game["source_count"], 2)
            self.assertEqual(game["developer"], "样例工作室")
            self.assertEqual(game["category"], "策略")
            audit = audit_catalog_completeness(conn)
            self.assertEqual(audit["total"], 1)
            self.assertEqual(audit["coverage"]["developer"]["rate"], 100.0)
            self.assertEqual(audit["coverage"]["long_description"]["missing"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
