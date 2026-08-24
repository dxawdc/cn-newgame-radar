import unittest

from newgame_monitor.webapp import _latest_game_intro, _serialize
from newgame_monitor.enrichment import _find_taptap_detail_content


class WebAppTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
