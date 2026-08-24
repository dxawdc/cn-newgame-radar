import unittest
from datetime import date

from newgame_monitor.collectors import (
    _apple_candidate_is_recent,
    _apple_lookup_item,
    _parse_apple_legacy_rss,
    _parse_apple_marketing_feed,
    _parse_apple_room_ids,
    _parse_apple_room_links,
)


class AppleIOSCollectorTest(unittest.TestCase):
    def test_parse_legacy_rss(self):
        payload = {"feed": {"entry": [
            {"id": {"attributes": {"im:id": "123"}}},
            {"id": {"attributes": {"im:id": "456"}}},
        ]}}
        self.assertEqual(_parse_apple_legacy_rss(payload), ["123", "456"])

    def test_parse_marketing_feed(self):
        payload = {"feed": {"results": [{"id": "123"}, {"id": "456"}]}}
        self.assertEqual(_parse_apple_marketing_feed(payload), ["123", "456"])

    def test_parse_room_links_and_ids(self):
        html = b'''<section><a href="/cn/iphone/room/99"><h2 data-test-id="shelf-title">\xe7\x83\xad\xe9\x97\xa8\xe6\x96\xb0\xe6\xb8\xb8 Top30</h2></a>
        <a href="https://apps.apple.com/cn/app/demo/id123456789">Demo</a></section>'''
        self.assertEqual(
            _parse_apple_room_links(html),
            {"热门新游 Top30": "https://apps.apple.com/cn/iphone/room/99"},
        )
        self.assertEqual(_parse_apple_room_ids(html), ["123456789"])

    def test_old_chart_game_is_not_new_game(self):
        self.assertFalse(_apple_candidate_is_recent(
            date(2020, 1, 1), {"chart:top-free"}, date(2026, 8, 21),
        ))

    def test_lookup_maps_release_and_full_details(self):
        detail = {
            "wrapperType": "software", "trackId": 123, "trackName": "样例新游",
            "bundleId": "com.example.game", "sellerName": "样例厂商",
            "primaryGenreName": "Games", "genres": ["Games", "Role Playing"],
            "releaseDate": "2026-08-20T07:00:00Z", "description": "完整游戏介绍" * 30,
            "artworkUrl512": "https://example.com/icon.png", "trackViewUrl": "https://apps.apple.com/cn/app/id123",
            "averageUserRating": 4.6, "version": "1.0.0", "fileSizeBytes": "1024",
        }
        item = _apple_lookup_item(detail, {"rss:new-apps", "editorial:热门新游 Top30"}, date(2026, 8, 21))
        self.assertIsNotNone(item)
        self.assertEqual(item["source"], "apple_appstore_cn")
        self.assertEqual(item["event_type"], "launch")
        self.assertEqual(item["event_time"], "2026-08-20")
        self.assertEqual(item["category"], "角色扮演")
        self.assertEqual(item["developer"], "样例厂商")
        self.assertIn("Apple 热门新游", item["tags"])

    def test_future_release_is_reservation(self):
        detail = {
            "wrapperType": "software", "trackId": 456, "trackName": "未来新游",
            "primaryGenreName": "Games", "genres": ["Games", "Strategy"],
            "releaseDate": "2026-09-10T07:00:00Z",
        }
        item = _apple_lookup_item(detail, {"rss:new-apps"}, date(2026, 8, 21))
        self.assertEqual(item["event_type"], "reservation")
        self.assertEqual(item["event_time"], "2026-09-10")


if __name__ == "__main__":
    unittest.main()
