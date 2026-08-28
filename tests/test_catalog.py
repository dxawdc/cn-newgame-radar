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
            "幻化竞技场（首发上线）": "幻化竞技场",
            "星海计划-公测上线": "星海计划",
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

    def test_all_products_use_structural_name_normalization_before_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            rows = [
                {
                    "source": "taptap", "source_item_id": "tap-1",
                    "name": "银河旅人", "developer": "星河工作室",
                    "event_type": "reservation",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "oppo-1",
                    "name": "银河旅人（全新科幻冒险手游）",
                    "developer": "星河工作室", "event_type": "reservation",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "honor_gamecenter", "source_item_id": "honor-1",
                    "name": "银河旅人-沉浸式星际探索",
                    "developer": "星河工作室", "event_type": "beta",
                    "event_time": "2026-09-02", "raw": {},
                },
            ]
            upsert_items(conn, rows, observed)
            self.assertTrue(all(
                row["canonical_key"]
                for row in conn.execute("SELECT canonical_key FROM source_items")
            ))
            self.assertEqual(rebuild_catalog(conn), 1)
            game = conn.execute("SELECT * FROM canonical_games").fetchone()
            self.assertEqual(game["canonical_key"], "name:银河旅人")
            self.assertEqual(game["name"], "银河旅人")
            self.assertEqual(game["source_count"], 3)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM canonical_members").fetchone()[0], 3,
            )
            conn.close()

    def test_verified_ambiguous_variants_merge_without_plain_name(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "oppo_gamecenter", "source_item_id": "oppo-1",
                    "name": "王者守卫战-经典三职业屠龙", "event_type": "pre_download",
                    "event_time": "2026-08-27", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "oppo-2",
                    "name": "王者守卫战（经典三职业屠龙传奇）", "event_type": "launch",
                    "event_time": "2026-08-28", "raw": {},
                },
            ], observed)
            self.assertEqual(rebuild_catalog(conn), 1)
            game = conn.execute("SELECT * FROM canonical_games").fetchone()
            self.assertEqual(game["canonical_key"], "name:王者守卫战")
            self.assertEqual(game["name"], "王者守卫战")
            conn.close()

    def test_suffix_similarity_does_not_merge_independent_editions(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "diablo-base",
                    "name": "暗黑破坏神", "package_name": "com.blizzard.diablo",
                    "developer": "Blizzard", "event_type": "launch",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "diablo-immortal",
                    "name": "暗黑破坏神-不朽",
                    "package_name": "com.netease.diabloimmortal",
                    "developer": "Blizzard", "event_type": "launch",
                    "event_time": "2026-09-02", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "pokemon-scarlet",
                    "name": "宝可梦-朱", "event_type": "launch",
                    "event_time": "2026-09-03", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "pokemon-violet",
                    "name": "宝可梦-紫", "event_type": "launch",
                    "event_time": "2026-09-03", "raw": {},
                },
            ], observed)
            self.assertEqual(rebuild_catalog(conn), 4)
            keys = {
                row["canonical_key"]
                for row in conn.execute("SELECT canonical_key FROM canonical_games")
            }
            self.assertIn("name:暗黑破坏神", keys)
            self.assertIn("name:暗黑破坏神不朽", keys)
            self.assertIn("name:宝可梦朱", keys)
            self.assertIn("name:宝可梦紫", keys)
            conn.close()

    def test_cross_source_marketing_suffix_alone_is_not_merge_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "pokemon-base",
                    "name": "宝可梦", "package_name": "com.example.pokemon.main",
                    "developer": "甲工作室", "event_type": "launch",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "pokemon-card",
                    "name": "宝可梦-卡牌", "package_name": "com.example.pokemon.card",
                    "developer": "乙工作室", "event_type": "launch",
                    "event_time": "2026-09-02", "raw": {},
                },
                {
                    "source": "taptap", "source_item_id": "cod-base",
                    "name": "使命召唤", "package_name": "com.example.cod.pc",
                    "developer": "丙工作室", "event_type": "launch",
                    "event_time": "2026-09-03", "raw": {},
                },
                {
                    "source": "honor_gamecenter", "source_item_id": "cod-mobile",
                    "name": "使命召唤-手游", "package_name": "com.example.cod.mobile",
                    "developer": "丁工作室", "event_type": "launch",
                    "event_time": "2026-09-04", "raw": {},
                },
            ], observed)
            self.assertEqual(rebuild_catalog(conn), 4)
            names = {
                row["name"] for row in conn.execute("SELECT name FROM canonical_games")
            }
            self.assertEqual(
                names, {"宝可梦", "宝可梦-卡牌", "使命召唤", "使命召唤-手游"},
            )
            conn.close()

    def test_same_developer_does_not_merge_distinct_items_in_one_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "oppo_gamecenter", "source_item_id": "star-base",
                    "name": "星海计划", "package_name": "com.vendor.star.base",
                    "developer": "同一开发商", "event_type": "launch",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "star-card",
                    "name": "星海计划-卡牌", "package_name": "com.vendor.star.cards",
                    "developer": "同一开发商", "event_type": "launch",
                    "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            self.assertEqual(rebuild_catalog(conn), 2)
            self.assertEqual(
                {
                    row["canonical_key"]
                    for row in conn.execute("SELECT canonical_key FROM canonical_games")
                },
                {"name:星海计划", "name:星海计划卡牌"},
            )
            conn.close()

    def test_stable_package_family_can_confirm_unusual_name_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "xiaomi_gamecenter", "source_item_id": "nova-mi",
                    "name": "星际远征", "package_name": "com.example.nova.mi",
                    "event_type": "reservation", "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "vivo_gamecenter", "source_item_id": "nova-vivo",
                    "name": "星际远征-Project Nova", "package_name": "com.example.nova.vivo",
                    "event_type": "beta", "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            self.assertEqual(rebuild_catalog(conn), 1)
            game = conn.execute("SELECT * FROM canonical_games").fetchone()
            self.assertEqual(game["canonical_key"], "name:星际远征")
            self.assertEqual(game["source_count"], 2)
            conn.close()

    def test_single_unconfirmed_subtitle_is_not_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "tap-1",
                "name": "剑定武林（像素江湖）", "event_type": "reservation",
                "event_time": "2026-09-01", "raw": {},
            }], "2026-08-27T10:00:00+08:00")
            self.assertEqual(rebuild_catalog(conn), 1)
            game = conn.execute("SELECT * FROM canonical_games").fetchone()
            self.assertEqual(game["canonical_key"], "name:剑定武林像素江湖")
            self.assertEqual(game["name"], "剑定武林(像素江湖)")
            conn.close()

    def test_name_merge_migrates_favorites_and_activity_history(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "tap-1",
                    "name": "银河旅人", "developer": "星河工作室",
                    "event_type": "reservation",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "oppo-1",
                    "name": "银河旅人（全新科幻冒险手游）",
                    "developer": "星河工作室", "event_type": "reservation",
                    "event_time": "2026-09-01", "raw": {},
                },
            ], observed)
            user_id = conn.execute(
                """
                INSERT INTO users(
                    username,normalized_username,display_name,password_hash,role,
                    created_at,updated_at
                ) VALUES ('tester','tester','测试用户','hash','user',?,?)
                """,
                (observed, observed),
            ).lastrowid
            old_key = conn.execute(
                "SELECT canonical_key FROM source_items WHERE source='oppo_gamecenter'"
            ).fetchone()[0]
            new_key = "name:银河旅人"
            conn.execute(
                "INSERT INTO user_favorites VALUES (?,?,?,?)",
                (user_id, new_key, "2026-08-27T09:00:00+08:00", observed),
            )
            conn.execute(
                "INSERT INTO user_favorites VALUES (?,?,?,?)",
                (
                    user_id, old_key, "2026-08-26T09:00:00+08:00",
                    "2026-08-27T11:00:00+08:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO favorite_activity_logs(user_id,game_key,action,occurred_at)
                VALUES (?,?,'follow',?)
                """,
                (user_id, old_key, observed),
            )
            conn.commit()

            rebuild_catalog(conn)
            favorites = list(conn.execute(
                "SELECT * FROM user_favorites WHERE user_id=?", (user_id,)
            ))
            self.assertEqual(len(favorites), 1)
            self.assertEqual(favorites[0]["game_key"], new_key)
            self.assertEqual(favorites[0]["created_at"], "2026-08-26T09:00:00+08:00")
            self.assertEqual(favorites[0]["last_followed_at"], "2026-08-27T11:00:00+08:00")
            activity_key = conn.execute(
                "SELECT game_key FROM favorite_activity_logs"
            ).fetchone()[0]
            self.assertEqual(activity_key, old_key)
            redirect = conn.execute(
                "SELECT new_key,reason FROM canonical_key_redirects WHERE old_key=?",
                (old_key,),
            ).fetchone()
            self.assertEqual(redirect["new_key"], new_key)
            self.assertEqual(redirect["reason"], "normalized_name_merge")
            conn.close()

    def test_rebuild_preserves_public_game_id(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "stable-id",
                "name": "稳定产品", "event_type": "reservation",
                "event_time": "2026-09-01", "raw": {},
            }], "2026-08-27T10:00:00+08:00")
            rebuild_catalog(conn)
            first_id = conn.execute("SELECT id FROM canonical_games").fetchone()[0]
            rebuild_catalog(conn)
            second_id = conn.execute("SELECT id FROM canonical_games").fetchone()[0]
            self.assertEqual(second_id, first_id)
            conn.close()

    def test_name_merge_redirects_removed_public_game_id(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "variant-only",
                "name": "银河旅人-科幻冒险手游", "developer": "星河工作室",
                "event_type": "reservation", "event_time": "2026-09-01", "raw": {},
            }], observed)
            rebuild_catalog(conn)
            old_id = conn.execute("SELECT id FROM canonical_games").fetchone()[0]

            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "base-later",
                "name": "银河旅人", "developer": "星河工作室",
                "event_type": "beta", "event_time": "2026-09-02", "raw": {},
            }], observed)
            rebuild_catalog(conn)
            game = conn.execute("SELECT id,canonical_key FROM canonical_games").fetchone()
            self.assertNotEqual(game["id"], old_id)
            self.assertEqual(game["canonical_key"], "name:银河旅人")
            redirect = conn.execute(
                "SELECT new_game_id FROM canonical_game_id_redirects WHERE old_game_id=?",
                (old_id,),
            ).fetchone()
            self.assertEqual(redirect["new_game_id"], game["id"])
            conn.close()

    def test_rebuild_quarantines_ambiguous_split_without_orphaning_favorite(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "old-1", "name": "旧产品",
                    "event_type": "reservation", "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "old-2", "name": "旧产品",
                    "event_type": "launch", "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            rebuild_catalog(conn)
            old_key = "name:旧产品"
            user_id = conn.execute(
                """
                INSERT INTO users(
                    username,normalized_username,display_name,password_hash,role,
                    created_at,updated_at
                ) VALUES ('splitter','splitter','拆分测试','hash','user',?,?)
                """,
                (observed, observed),
            ).lastrowid
            conn.execute(
                "INSERT INTO user_favorites VALUES (?,?,?,?)",
                (user_id, old_key, observed, observed),
            )
            conn.execute("UPDATE source_items SET name='产品甲' WHERE source='taptap'")
            conn.execute("UPDATE source_items SET name='产品乙' WHERE source='oppo_gamecenter'")
            conn.commit()

            rebuild_catalog(conn)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM canonical_games WHERE canonical_key=?", (old_key,)
            ).fetchone())
            self.assertEqual(conn.execute(
                "SELECT game_key FROM user_favorites WHERE user_id=?", (user_id,)
            ).fetchone()[0], old_key)
            quarantine = conn.execute(
                "SELECT reason,status FROM catalog_quarantine WHERE issue_key=?", (old_key,)
            ).fetchone()
            self.assertEqual(dict(quarantine), {"reason": "one_to_many_split", "status": "active"})
            conn.close()

    def test_rebuild_quarantines_split_when_one_target_keeps_old_key(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "old-1", "name": "旧产品",
                    "developer": "甲工作室", "event_type": "reservation",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "old-2", "name": "旧产品",
                    "developer": "乙工作室", "event_type": "launch",
                    "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            rebuild_catalog(conn)
            conn.execute(
                "UPDATE source_items SET name='旧产品-卡牌' WHERE source='oppo_gamecenter'"
            )
            conn.commit()

            rebuild_catalog(conn)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM canonical_games").fetchone()[0], 1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT canonical_key FROM canonical_games"
                ).fetchone()[0],
                "name:旧产品",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM catalog_quarantine WHERE issue_key='name:旧产品'"
                ).fetchone()[0],
                "active",
            )
            conn.close()

    def test_rebuild_quarantines_redirect_cycle_and_keeps_other_catalog_available(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "swap-a", "name": "产品甲",
                    "event_type": "reservation", "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "swap-b", "name": "产品乙",
                    "event_type": "launch", "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            rebuild_catalog(conn)
            conn.execute(
                "UPDATE source_items SET name=CASE source_item_id "
                "WHEN 'swap-a' THEN '产品乙' ELSE '产品甲' END"
            )
            conn.commit()

            rebuild_catalog(conn)
            self.assertEqual(
                {
                    row["canonical_key"]
                    for row in conn.execute("SELECT canonical_key FROM canonical_games")
                },
                {"name:产品甲", "name:产品乙"},
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM canonical_key_redirects").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM catalog_quarantine WHERE status='active' AND reason='redirect_cycle'"
                ).fetchone()[0],
                2,
            )
            conn.close()

    def test_rebuild_flattens_redirect_chains(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-27T10:00:00+08:00"
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "rename-chain", "name": "产品甲",
                "event_type": "reservation", "event_time": "2026-09-01", "raw": {},
            }], observed)
            rebuild_catalog(conn)
            conn.execute("UPDATE source_items SET name='产品乙'")
            conn.commit()
            rebuild_catalog(conn)
            conn.execute("UPDATE source_items SET name='产品丙'")
            conn.commit()
            rebuild_catalog(conn)

            redirects = {
                row["old_key"]: row["new_key"]
                for row in conn.execute(
                    "SELECT old_key,new_key FROM canonical_key_redirects"
                )
            }
            self.assertEqual(
                redirects,
                {"name:产品甲": "name:产品丙", "name:产品乙": "name:产品丙"},
            )
            current_id = conn.execute("SELECT id FROM canonical_games").fetchone()[0]
            id_redirects = {
                row["old_game_id"]: row["new_game_id"]
                for row in conn.execute(
                    "SELECT old_game_id,new_game_id FROM canonical_game_id_redirects"
                )
            }
            self.assertEqual(set(id_redirects.values()), {current_id})
            self.assertEqual(len(id_redirects), 2)
            conn.close()


if __name__ == "__main__":
    unittest.main()
