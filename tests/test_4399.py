import unittest
from datetime import datetime

from newgame_monitor.collectors import _parse_4399_event_date


class GameBox4399DateTest(unittest.TestCase):
    def test_normalizes_only_dates_with_explicit_year(self):
        observed = datetime.fromisoformat("2026-08-20T10:00:00+08:00")
        self.assertEqual(_parse_4399_event_date("2026年8月28日", observed), "2026-08-28")
        self.assertEqual(_parse_4399_event_date("12-31", observed), "")
        self.assertEqual(_parse_4399_event_date("01-15", observed), "")

    def test_does_not_invent_day_for_month_only_or_unknown(self):
        observed = datetime.fromisoformat("2026-08-20T10:00:00+08:00")
        self.assertEqual(_parse_4399_event_date("2026年9月", observed), "")
        self.assertEqual(_parse_4399_event_date("敬请期待", observed), "")


if __name__ == "__main__":
    unittest.main()
