import json
import unittest

from newgame_monitor.gallery import extract_gallery_urls
from newgame_monitor.webapp import _serialize


class GalleryAndLinksTest(unittest.TestCase):
    def test_extract_known_store_screenshots(self):
        tap = {
            "app_card_info": {
                "screenshots": [
                    {"url": "https://img.example/tap-1.jpg"},
                    {"url": "https://img.example/tap-2.jpg"},
                ]
            }
        }
        vivo = {"burstInfo": {"screenShots": ["https://img.example/vivo-1.jpg"]}}
        self.assertEqual(
            extract_gallery_urls("taptap", tap),
            ["https://img.example/tap-1.jpg", "https://img.example/tap-2.jpg"],
        )
        self.assertEqual(
            extract_gallery_urls("vivo_gamecenter", vivo),
            ["https://img.example/vivo-1.jpg"],
        )
        self.assertEqual(
            extract_gallery_urls("uc_9game", {
                "detail": {"screenshot_urls": ["https://img.example/9game-1.jpg"]}
            }),
            ["https://img.example/9game-1.jpg"],
        )
        self.assertEqual(
            extract_gallery_urls("xiaomi_gamecenter", {
                "official_public_detail": {
                    "screenshots": ["https://img.example/xiaomi-1.jpg"]
                }
            }),
            ["https://img.example/xiaomi-1.jpg"],
        )
        self.assertEqual(
            extract_gallery_urls("haoyou_kuaibao", {
                "detail_screenshot_urls": ["https://img.example/haoyou-1.jpg"]
            }),
            ["https://img.example/haoyou-1.jpg"],
        )
        self.assertEqual(
            extract_gallery_urls("oppo_gamecenter", {
                "ui_detail": {
                    "screenshot_urls": [
                        "local-screenshot://gallery/oppo_gamecenter/sample.webp"
                    ]
                }
            }),
            ["local-screenshot://gallery/oppo_gamecenter/sample.webp"],
        )

    def test_real_screenshots_precede_marketing_fallbacks(self):
        tap = {
            "detail_screenshots": [{"url": "https://img.example/tap-detail.jpg"}],
            "app_card_info": {
                "screenshots": [{"url": "https://img.example/tap-list.jpg"}],
                "banner": {"url": "https://img.example/tap-banner.jpg"},
            },
        }
        self.assertEqual(extract_gallery_urls("taptap", tap), [
            "https://img.example/tap-detail.jpg",
            "https://img.example/tap-list.jpg",
        ])
        self.assertEqual(extract_gallery_urls("taptap", {
            "app_card_info": {"banner": {"url": "https://img.example/tap-banner.jpg"}}
        }), ["https://img.example/tap-banner.jpg"])
        self.assertEqual(extract_gallery_urls("vivo_gamecenter", {
            "additionalImages": {"waterfall": "https://img.example/vivo-waterfall.jpg"},
            "video": {"videoImageUrl": "https://img.example/vivo-video.jpg"},
        }), [
            "https://img.example/vivo-waterfall.jpg",
            "https://img.example/vivo-video.jpg",
        ])

    def test_app_only_store_link_is_hidden_but_gallery_remains(self):
        base = {
            "event_type": "launch", "event_time": "2026-08-24",
            "event_end_time": "", "status": "首发",
            "first_seen_at": "2026-08-24T06:00:00+08:00",
        }
        members = [
            {
                **base, "source": "oppo_gamecenter",
                "detail_url": "oppomarket://details?id=1", "raw_json": "{}",
            },
            {
                **base, "source": "vivo_gamecenter",
                "detail_url": "https://gamecenter.vivo.example/app/1",
                "raw_json": json.dumps({
                    "burstInfo": {"screenShots": ["https://img.example/vivo-1.jpg"]}
                }),
            },
            {
                **base, "source": "taptap",
                "detail_url": "https://www.taptap.cn/app/1", "raw_json": "{}",
            },
        ]
        game = {
            "id": 1, "canonical_key": "name:示例", "name": "示例",
            "developer": "工作室", "category": "角色扮演", "tags_json": "[]",
            "gameplay_intro": "介绍", "icon_url": "", "rating": None,
            "first_seen_at": base["first_seen_at"], "last_seen_at": base["first_seen_at"],
            "members": members,
        }
        payload = _serialize(game)
        by_source = {event["source"]: event["detail_url"] for event in payload["events"]}
        self.assertIsNone(by_source["oppo_gamecenter"])
        self.assertIsNone(by_source["vivo_gamecenter"])
        self.assertEqual(by_source["taptap"], "https://www.taptap.cn/app/1")
        self.assertEqual(payload["gallery"][0]["source"], "vivo_gamecenter")


if __name__ == "__main__":
    unittest.main()
