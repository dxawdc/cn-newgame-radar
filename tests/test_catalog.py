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
        self.assertEqual(clean_game_name("头号禁区（限时开测）"), "头号禁区")
        self.assertEqual(clean_game_name("绝境猎人（西部狂欢热）"), "绝境猎人")
        self.assertEqual(clean_game_name("开天英雄(安装赢HUAWEI Mate80 Pro)"), "开天英雄")
        self.assertEqual(normalize_game_name("光 · 遇"), normalize_game_name("光·遇"))

    def test_activity_suffix_grammar_covers_products_without_manual_aliases(self):
        cases = {
            "大梦仙途（删档测试）": "大梦仙途",
            "笑笑四川麻将（删档计费测试）": "笑笑四川麻将",
            "燃烧纪元（8月20日删测开启）": "燃烧纪元",
            "山海大冒险-8.28测试开启": "山海大冒险",
            "客官里面请-删档不计费测试": "客官里面请",
            "重生:我觉醒无限异能-删档测试": "重生:我觉醒无限异能",
            "冲啊,逗英雄-公测送千抽": "冲啊,逗英雄",
            "星海计划(开启限量测试)": "星海计划",
            "星海计划-2026-08-20上线": "星海计划",
            "星海计划(现已开启预约)": "星海计划",
            "星海计划(今日10:00开测)": "星海计划",
            "星海计划【限时开测!】": "星海计划",
            "天命行者(登陆送1000连抽)": "天命行者",
            "古剑奇闻录(送真充)": "古剑奇闻录",
            "防线出击-送10000抽": "防线出击",
            "影之刃零-预购已开启-PC": "影之刃零",
            "女神异闻录4 Revival-PC端": "女神异闻录4 Revival",
            "使命召唤手游-崩坏3联动开启": "使命召唤手游",
            "燕云十六声-8月新版本": "燕云十六声",
            "三国志·战略版-7周年庆": "三国志·战略版",
        }
        for raw_name, expected in cases.items():
            with self.subTest(raw_name=raw_name):
                self.assertEqual(clean_game_name(raw_name), expected)

    def test_normal_titles_are_not_stripped_as_activity_suffixes(self):
        names = (
            "极限生存（机械狂欢1）",
            "剑定武林（像素江湖）",
            "飞越13号房（原始版）",
            "土豆兄弟-Brotato",
            "测试人生（重制版）",
            "领航员物语(首发领航员物语)",
            "得分王-上线得分王",
        )
        for raw_name in names:
            with self.subTest(raw_name=raw_name):
                self.assertEqual(clean_game_name(raw_name), raw_name.translate(str.maketrans("（）：，", "():,")))

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
            self.assertEqual(game["event_count"], 2)
            self.assertEqual(game["developer"], "样例工作室")
            self.assertEqual(game["category"], "策略")
            audit = audit_catalog_completeness(conn)
            self.assertEqual(audit["total"], 1)
            self.assertEqual(audit["coverage"]["developer"]["rate"], 100.0)
            self.assertEqual(audit["coverage"]["long_description"]["missing"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
