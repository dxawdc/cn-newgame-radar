import unittest

from newgame_monitor.collectors import _parse_xiaomi_discovery


class XiaomiDiscoveryTest(unittest.TestCase):
    def test_parses_public_beta_zone_payload(self):
        payload = {
            "data": {"blocks": [{"id": "10003019", "list": [{
                "id": 62430001,
                "title": "样例新游",
                "summary": "测试简介",
                "actUrl": "migamecenter://game_info_act?gameId=62430001",
                "tag": [{"name": "卡牌"}, {"name": "策略"}],
                "dInfo": {
                    "icon": "https://example.com/icon.png",
                    "developer_name": "样例厂商",
                    "apk": {"packageName": "com.example.game", "apkSize": 123},
                    "testing": {"name": "限量删档测试", "begin": 1787191200},
                },
            }]}]},
        }

        items = _parse_xiaomi_discovery(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_item_id"], "62430001")
        self.assertEqual(items[0]["developer"], "样例厂商")
        self.assertEqual(items[0]["category"], "卡牌")
        self.assertEqual(items[0]["event_type"], "beta")
        self.assertTrue(items[0]["event_time"].startswith("2026-08-20"))


if __name__ == "__main__":
    unittest.main()
