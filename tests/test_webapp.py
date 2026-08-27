import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from newgame_monitor import webapp
from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import connect, upsert_items
from newgame_monitor.webapp import _effective_event_type, _latest_game_intro, _serialize
from newgame_monitor.enrichment import _find_taptap_detail_content


class WebAppTest(unittest.TestCase):
    def test_undated_launch_is_exposed_as_first_seen(self):
        member = {
            "source": "uc_9game", "event_type": "launch", "event_time": "",
            "event_end_time": "", "status": "首发", "detail_url": None,
            "first_seen_at": "2026-08-21T10:00:00+08:00", "raw_json": "{}",
        }
        game = {
            "id": 1, "canonical_key": "name:待定档新游", "name": "待定档新游",
            "developer": "样例公司", "category": "策略", "tags_json": "[]",
            "gameplay_intro": "样例介绍", "icon_url": "", "rating": None,
            "first_seen_at": member["first_seen_at"], "last_seen_at": member["first_seen_at"],
            "members": [member],
        }

        payload = _serialize(game)

        self.assertEqual(_effective_event_type(member), "first_seen")
        self.assertEqual(payload["events"][0]["type"], "first_seen")
        self.assertEqual(payload["events"][0]["type_label"], "首次采集发现")
        self.assertEqual(payload["events"][0]["date_precision"], "discovered")

    def test_apple_source_is_last_and_marked_incomplete(self):
        members = [
            {
                "source": "apple_appstore_cn", "event_type": "launch",
                "event_time": "2026-08-20", "event_end_time": "",
                "status": "App Store 上架", "detail_url": None,
                "first_seen_at": "2026-08-21T10:00:00+08:00",
            },
            {
                "source": "taptap", "event_type": "launch",
                "event_time": "2026-08-20", "event_end_time": "",
                "status": "上线", "detail_url": None,
                "first_seen_at": "2026-08-21T10:00:00+08:00",
            },
        ]
        game = {
            "id": 1, "canonical_key": "name:样例新游", "name": "样例新游",
            "developer": "样例公司", "category": "角色扮演", "tags_json": "[]",
            "gameplay_intro": "样例介绍", "icon_url": "", "rating": None,
            "first_seen_at": "2026-08-21T10:00:00+08:00",
            "last_seen_at": "2026-08-21T10:00:00+08:00", "members": members,
        }
        result = _serialize(game)
        self.assertEqual([source["key"] for source in result["sources"]], ["taptap", "apple_appstore_cn"])
        self.assertEqual(result["sources"][-1]["note"], "数据不全")
        apple_event = next(event for event in result["events"] if event["source"] == "apple_appstore_cn")
        self.assertEqual(apple_event["source_note"], "数据不全")

    def test_events_sort_by_complete_date_across_years(self):
        members = [
            {
                "source": "xiaomi_gamecenter", "event_type": "reservation",
                "event_time": "2027-04-30T10:00:00+08:00", "event_end_time": "",
                "status": "敬请期待", "detail_url": None,
                "first_seen_at": "2026-08-19T10:00:00+08:00",
            },
            {
                "source": "oppo_gamecenter", "event_type": "beta",
                "event_time": "2026-08-20", "event_end_time": "",
                "status": "内测", "detail_url": None,
                "first_seen_at": "2026-08-19T10:00:00+08:00",
            },
        ]
        game = {
            "id": 1, "canonical_key": "name:佣兵大冒险", "name": "佣兵大冒险",
            "developer": "样例公司", "category": "角色扮演", "tags_json": "[]",
            "gameplay_intro": "样例介绍", "icon_url": "", "rating": None,
            "first_seen_at": "2026-08-19T10:00:00+08:00",
            "last_seen_at": "2026-08-20T10:00:00+08:00", "members": members,
        }
        result = _serialize(game)
        self.assertEqual(
            [event["date"] for event in result["events"]],
            ["2026-08-20", "2027-04-30"],
        )

    def test_taptap_detail_uses_untruncated_content_node(self):
        root = {
            "app": {"description": {"text": "截断介绍..."}},
            "detail": {"description": {"text": "完整游戏介绍" * 80}},
        }
        result = _find_taptap_detail_content(root)
        self.assertEqual(result["description"]["text"], "完整游戏介绍" * 80)

    def test_latest_intro_skips_event_notifications(self):
        game = {
            "gameplay_intro": "聚合介绍",
            "members": [
                {
                    "source": "taptap", "gameplay_intro": "少年JUMP正版授权全明星3v3乱斗",
                    "full_description": None,
                    "status": "限量测试", "last_seen_at": "2026-08-19T20:00:00+08:00",
                    "detail_url": "https://example.com/taptap",
                },
                {
                    "source": "haoyou_kuaibao", "gameplay_intro": "10:00 限量测试",
                    "full_description": None,
                    "status": "10:00 限量测试", "last_seen_at": "2026-08-20T10:00:00+08:00",
                    "detail_url": "https://example.com/3839",
                },
            ],
        }
        result = _latest_game_intro(game)
        self.assertEqual(result["text"], "少年JUMP正版授权全明星3v3乱斗")
        self.assertEqual(result["source_label"], "TapTap")
        self.assertEqual(result["collected_at"], "2026-08-19T20:00:00+08:00")

    def test_latest_valid_description_wins(self):
        game = {
            "gameplay_intro": "聚合介绍",
            "members": [
                {
                    "source": "vivo_gamecenter", "gameplay_intro": "旧版游戏介绍",
                    "full_description": None,
                    "status": "预约", "last_seen_at": "2026-08-19T20:00:00+08:00", "detail_url": None,
                },
                {
                    "source": "honor_gamecenter", "gameplay_intro": "最近更新的游戏介绍",
                    "full_description": None,
                    "status": "精品首发", "last_seen_at": "2026-08-20T10:00:00+08:00", "detail_url": None,
                },
            ],
        }
        result = _latest_game_intro(game)
        self.assertEqual(result["text"], "最近更新的游戏介绍")
        self.assertEqual(result["source_label"], "荣耀游戏中心")

    def test_full_description_beats_shorter_summary(self):
        game = {
            "gameplay_intro": "短简介",
            "members": [
                {
                    "source": "huawei_gamecenter", "gameplay_intro": "短简介",
                    "full_description": "第一段完整介绍。\n\n第二段玩法详情。" * 20,
                    "status": "预约", "last_seen_at": "2026-08-19T20:00:00+08:00", "detail_url": None,
                },
                {
                    "source": "honor_gamecenter", "gameplay_intro": "最近采集的短简介",
                    "full_description": None,
                    "status": "首发", "last_seen_at": "2026-08-20T10:00:00+08:00", "detail_url": None,
                },
            ],
        }
        result = _latest_game_intro(game)
        self.assertEqual(result["kind"], "full")
        self.assertEqual(result["source_label"], "华为游戏中心")


class GameIdRedirectTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.original_db = webapp.DB_PATH
        webapp.DB_PATH = Path(self.temporary.name) / "redirects.db"
        conn = connect(webapp.DB_PATH)
        upsert_items(conn, [{
            "source": "oppo_gamecenter",
            "source_item_id": "redirect-target",
            "name": "重定向目标游戏",
            "event_type": "launch",
            "event_time": "2026-08-27",
        }], "2026-08-27T08:00:00+08:00")
        rebuild_catalog(conn)
        self.current_id = conn.execute("SELECT id FROM canonical_games").fetchone()[0]
        conn.close()

    def tearDown(self):
        webapp.DB_PATH = self.original_db
        self.temporary.cleanup()

    @staticmethod
    def _request(game_id: int) -> Request:
        return Request({
            "type": "http", "method": "GET", "path": f"/api/games/{game_id}",
            "headers": [], "query_string": b"",
        })

    def _insert_redirects(self, redirects: list[tuple[int, int]]) -> None:
        conn = connect(webapp.DB_PATH)
        conn.executemany(
            """
            INSERT INTO canonical_game_id_redirects(
                old_game_id,new_game_id,reason,created_at
            ) VALUES (?,?,'normalized_name_merge','2026-08-27T10:00:00+08:00')
            """,
            redirects,
        )
        conn.commit()
        conn.close()

    def test_game_detail_follows_old_id_redirect_chain(self):
        old_id = self.current_id + 1000
        middle_id = old_id + 1
        self._insert_redirects([
            (old_id, middle_id),
            (middle_id, self.current_id),
        ])

        payload = webapp.game_detail(old_id, self._request(old_id))

        self.assertEqual(payload["id"], self.current_id)
        self.assertEqual(payload["name"], "重定向目标游戏")

    def test_game_detail_rejects_redirect_cycle(self):
        first_id = self.current_id + 2000
        second_id = first_id + 1
        self._insert_redirects([
            (first_id, second_id),
            (second_id, first_id),
        ])

        payload = webapp.game_detail(first_id, self._request(first_id))

        self.assertEqual(payload, {"error": "not_found"})

    def test_game_detail_rejects_redirect_chain_beyond_limit(self):
        first_id = self.current_id + 3000
        redirects = []
        for offset in range(webapp.GAME_ID_REDIRECT_MAX_HOPS + 1):
            old_id = first_id + offset
            new_id = (
                self.current_id
                if offset == webapp.GAME_ID_REDIRECT_MAX_HOPS
                else old_id + 1
            )
            redirects.append((old_id, new_id))
        self._insert_redirects(redirects)

        payload = webapp.game_detail(first_id, self._request(first_id))

        self.assertEqual(payload, {"error": "not_found"})


class CatalogDimensionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.original_db = webapp.DB_PATH
        webapp.DB_PATH = Path(self.temporary.name) / "dimensions.db"
        conn = connect(webapp.DB_PATH)
        common = {
            "name": "双维度样例游戏", "developer": "样例工作室",
            "category": "策略", "gameplay_intro": "验证渠道与产品粒度。", "raw": {},
        }
        rows = [
            {**common, "source": "taptap", "source_item_id": "tap-1", "event_type": "launch", "event_time": "2026-08-23", "status": "上线"},
            {**common, "source": "xiaomi_gamecenter", "source_item_id": "mi-1", "event_type": "launch", "event_time": "2026-08-23", "status": "首发"},
            {**common, "source": "haoyou_kuaibao", "source_item_id": "hy-1", "event_type": "launch", "event_time": "2026-08-24", "status": "上线"},
            {**common, "source": "233_leyuan", "source_item_id": "233-1", "event_type": "beta", "event_time": "2026-08-22", "status": "测试"},
            {**common, "source": "uc_9game", "source_item_id": "uc-1", "event_type": "launch", "event_time": "", "status": "首次采集"},
        ]
        upsert_items(conn, rows, "2026-08-25T06:00:00+08:00")
        upsert_items(conn, [{
            **common, "source": "haoyou_kuaibao", "source_item_id": "hy-2",
            "event_type": "launch", "event_time": "2026-08-26", "status": "延期后上线",
        }], "2026-08-26T06:00:00+08:00")
        rebuild_catalog(conn)
        conn.close()

    def tearDown(self):
        webapp.DB_PATH = self.original_db
        self.temporary.cleanup()

    def test_product_view_uses_earliest_date_and_merges_same_day_channels(self):
        items = webapp._filtered_games("all", view_mode="product")
        launch = next(item for item in items if item["featured_event"]["type"] == "launch")
        self.assertEqual(launch["featured_event"]["date"], "2026-08-23")
        self.assertEqual(
            [source["key"] for source in launch["event_sources"]],
            ["taptap", "xiaomi_gamecenter"],
        )
        self.assertEqual(launch["later_event_count"], 1)

    def test_channel_view_keeps_each_source_event(self):
        items = webapp._filtered_games("all", view_mode="channel")
        launch_dates = sorted(
            item["featured_event"]["date"]
            for item in items
            if item["featured_event"]["type"] == "launch"
        )
        self.assertEqual(launch_dates, ["2026-08-23", "2026-08-23", "2026-08-26"])

    def test_source_scope_recalculates_product_earliest_date(self):
        items = webapp._filtered_games(
            "all", sources={"haoyou_kuaibao"}, view_mode="product"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["featured_event"]["date"], "2026-08-26")

    def test_same_product_event_source_keeps_latest_observation(self):
        items = webapp._filtered_games(
            "all", sources={"haoyou_kuaibao"}, event_types={"launch"}, view_mode="channel",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["featured_event"]["date"], "2026-08-26")
        self.assertEqual(items[0]["featured_event"]["status"], "延期后上线")

    def test_undated_and_dated_observations_share_raw_event_identity(self):
        rows = [
            {
                "id": 1, "source": "xiaomi_gamecenter", "event_type": "reservation",
                "event_time": "", "last_seen_at": "2026-08-25T06:00:00+08:00",
            },
            {
                "id": 2, "source": "xiaomi_gamecenter", "event_type": "reservation",
                "event_time": "2026-09-03", "last_seen_at": "2026-08-26T06:00:00+08:00",
            },
        ]
        latest = webapp._latest_event_rows(rows)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["id"], 2)

    def test_same_batch_earlier_date_revision_wins_by_row_revision(self):
        observed = "2026-08-27T06:00:00+08:00"
        rows = [
            {
                "id": 8, "source": "oppo_gamecenter", "event_type": "launch",
                "event_time": "2026-09-10", "last_seen_at": observed,
            },
            {
                "id": 9, "source": "oppo_gamecenter", "event_type": "launch",
                "event_time": "2026-09-05", "last_seen_at": observed,
            },
        ]
        latest = webapp._latest_event_rows(rows)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["id"], 9)

    def test_first_seen_filter_does_not_revive_superseded_undated_row(self):
        conn = connect(webapp.DB_PATH)
        common = {
            "name": "定档更新样例", "source": "xiaomi_gamecenter",
            "event_type": "reservation", "raw": {},
        }
        upsert_items(conn, [{
            **common, "source_item_id": "mi-undated", "event_time": "",
        }], "2026-08-25T06:00:00+08:00")
        upsert_items(conn, [{
            **common, "source_item_id": "mi-dated", "event_time": "2026-09-03",
        }], "2026-08-26T06:00:00+08:00")
        rebuild_catalog(conn)
        conn.close()

        stale = webapp._filtered_games(
            "all", event_types={"first_seen"}, q="定档更新样例", view_mode="channel",
        )
        self.assertEqual(stale, [])
        current = webapp._filtered_games(
            "all", event_types={"reservation"}, q="定档更新样例", view_mode="channel",
        )
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["featured_event"]["date"], "2026-09-03")

    def test_date_filter_runs_after_product_aggregation(self):
        items = webapp._filtered_games(
            "all", date_from="2026-08-24", date_to="2026-08-24",
            event_types={"launch"}, view_mode="product",
        )
        self.assertEqual(items, [])
        scoped = webapp._filtered_games(
            "all", date_from="2026-08-24", date_to="2026-08-24",
            sources={"haoyou_kuaibao"}, event_types={"launch"}, view_mode="product",
        )
        self.assertEqual(scoped, [])

    def test_different_event_types_and_first_seen_remain_separate(self):
        items = webapp._filtered_games("all", view_mode="product")
        by_type = {item["featured_event"]["type"]: item for item in items}
        self.assertEqual(set(by_type), {"beta", "launch", "first_seen"})
        self.assertEqual(by_type["beta"]["featured_event"]["date"], "2026-08-22")
        self.assertEqual(by_type["first_seen"]["featured_event"]["date"], "2026-08-25")

    def test_api_event_scope_excludes_first_seen_by_default_and_can_include_it(self):
        default_items = webapp._filtered_games(
            "all", event_types=webapp._event_scope(None), view_mode="product"
        )
        self.assertNotIn(
            "first_seen", {item["featured_event"]["type"] for item in default_items}
        )
        discovery_items = webapp._filtered_games(
            "all", event_types=webapp._event_scope(["first_seen"]), view_mode="product"
        )
        self.assertEqual(len(discovery_items), 1)
        self.assertEqual(discovery_items[0]["featured_event"]["type"], "first_seen")


if __name__ == "__main__":
    unittest.main()
