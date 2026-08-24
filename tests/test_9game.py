import unittest
from datetime import datetime, timezone

from newgame_monitor.collectors import (
    _classify_9game_event,
    _parse_9game_detail_html,
    _parse_9game_schedule_html,
)


class NineGameCollectorTest(unittest.TestCase):
    def test_classification_rejects_old_game_operations(self):
        self.assertEqual(_classify_9game_event("样例游戏", "首发"), "launch")
        self.assertEqual(_classify_9game_event("样例游戏", "计费删档内测"), "beta")
        self.assertIsNone(_classify_9game_event("样例游戏", "S8赛季更新"))
        self.assertIsNone(_classify_9game_event("样例游戏", "联动新活动"))

    def test_schedule_extracts_date_game_id_and_icon(self):
        html = """
        <div class="des-table1">
          <div class="day">明天</div>
          <table><tr>
            <td class="timetr"><span class="time">首发</span></td>
            <td class="nametr">
              <a class="name" data-statis="game-3121977" href="/yangli/" title="样例游戏">样例游戏（营销副标题）</a>
              <img src="placeholder.jpg" xlazyimg="https://example.com/icon.png">
            </td>
            <td class="stattr">首发</td><td class="typetr">策略</td>
          </tr></table>
        </div>
        """.encode("utf-8")
        observed = datetime(2026, 8, 21, 10, tzinfo=timezone.utc)
        items = _parse_9game_schedule_html(html, observed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "uc_9game")
        self.assertEqual(items[0]["source_item_id"], "3121977")
        self.assertEqual(items[0]["name"], "样例游戏")
        self.assertEqual(items[0]["event_time"], "2026-08-22")
        self.assertEqual(items[0]["event_type"], "launch")
        self.assertEqual(items[0]["icon_url"], "https://example.com/icon.png")

    def test_detail_extracts_developer_and_full_description(self):
        html = """
        <div class="ngame-dws">
          <h1 class="ngame-title"><a>样例游戏</a></h1>
          <div class="ngame-img"><img src="https://example.com/icon.png"></div>
          <div class="ngame-types">类型：<span class="point">策略</span></div>
          <div class="ngame-desc">短介绍
            <span class="more"><div class="tips"><p class="txt">
              这是一段足够长的完整产品介绍，包含世界观、核心玩法、角色成长、多人合作以及长期挑战目标。
              <div class="company">开发者：样例网络科技有限公司</div>
            </p></div></span>
          </div>
          <div class="special-img short">
            <span class="img"><img src="https://media.9game.cn/screen-1.jpg"></span>
            <span class="img"><img src="https://media.9game.cn/screen-2.jpg"></span>
          </div>
        </div>
        """.encode("utf-8")
        parsed = _parse_9game_detail_html(html)
        self.assertEqual(parsed["developer"], "样例网络科技有限公司")
        self.assertEqual(parsed["category"], "策略")
        self.assertIn("核心玩法", parsed["description"])
        self.assertNotIn("开发者", parsed["description"])
        self.assertEqual(parsed["screenshot_urls"], [
            "https://media.9game.cn/screen-1.jpg",
            "https://media.9game.cn/screen-2.jpg",
        ])


if __name__ == "__main__":
    unittest.main()
