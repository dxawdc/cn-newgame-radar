import unittest
from datetime import datetime

from newgame_monitor.collectors import _parse_233_event_date, _resolve_233_event_date


class Leyuan233DateTest(unittest.TestCase):
    def test_parser_accepts_detail_datetime(self):
        self.assertEqual(
            _parse_233_event_date("2026-08-21 09:00:00", None),
            "2026-08-21",
        )

    def test_launch_prefers_online_time_over_banner_campaign_time(self):
        campaign_start = int(datetime(2026, 8, 24, 6, 0).astimezone().timestamp() * 1000)
        actual = _resolve_233_event_date(
            "launch",
            "诡秘之主-下载送乐币 公测全民送首充",
            {"effectiveTimeBegin": campaign_start},
            {"onlineTime": "2026-08-21 09:00:00"},
        )
        self.assertEqual(actual, "2026-08-21")

    def test_old_game_online_time_does_not_replace_current_candidate(self):
        campaign_start = int(datetime(2026, 8, 24, 6, 0).astimezone().timestamp() * 1000)
        actual = _resolve_233_event_date(
            "launch",
            "老游戏运营内容上线",
            {"effectiveTimeBegin": campaign_start},
            {"onlineTime": "2023-11-03 10:00:00"},
        )
        self.assertEqual(actual, "2026-08-24")


if __name__ == "__main__":
    unittest.main()
