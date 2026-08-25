import json
import tempfile
import unittest
from pathlib import Path

from newgame_monitor.db import connect, upsert_items
from newgame_monitor.event_quality import (
    classify_haoyou_event,
    prune_legacy_haoyou_timeline,
    repair_233_launch_dates,
)


class EventQualityTest(unittest.TestCase):
    def test_new_game_events_are_classified(self):
        cases = {
            ("诡秘之主-预下载(官服)", "预下载已开启！8月21日上线"): "pre_download",
            ("赘婿", "10:00 正式上线"): "launch",
            ("王权：星途", "海外上线安卓版"): "launch",
            ("卧龙三国", "10:00 删档测试"): "beta",
            ("阿索拉：星之祈愿", "11:00 开启海外首测招募"): "recruiting_beta",
            ("深空之劫", "10:00 限量抢注测试"): "limited_beta",
            ("北境之地", "正式开启预约，上线时间待定"): "reservation",
        }
        for (name, summary), expected in cases.items():
            with self.subTest(name=name, summary=summary):
                self.assertEqual(classify_haoyou_event(name, summary), expected)

    def test_old_game_operations_are_rejected(self):
        cases = (
            ("使命召唤手游", "亡灵系列武器上架，活动送紫枪"),
            ("使命召唤手游", "七夕对局领史诗武器"),
            ("使命召唤手游体验服", "体验服开服，具体几点待定"),
            ("海岛奇兵", "新战令民房皮肤【热带】上线"),
            ("原神(官服)-至冬开放", "新国度至冬开放，新角色登场"),
            ("逆战：未来-S3赛季", "《战双帕弥什》联动开启"),
            ("部落冲突-14周年", "PVE玩法正式上线"),
        )
        for name, summary in cases:
            with self.subTest(name=name, summary=summary):
                self.assertIsNone(classify_haoyou_event(name, summary))

    def test_legacy_timeline_rows_are_pruned_or_reclassified(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-20T10:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "haoyou_kuaibao", "source_item_id": "old",
                    "name": "使命召唤手游", "gameplay_intro": "七夕对局领史诗武器",
                    "event_type": "timeline", "event_time": "2026-08-20", "raw": {},
                },
                {
                    "source": "haoyou_kuaibao", "source_item_id": "new",
                    "name": "样例新游", "gameplay_intro": "10:00 正式上线",
                    "event_type": "timeline", "event_time": "2026-08-20", "raw": {},
                },
            ], observed)
            result = prune_legacy_haoyou_timeline(conn)
            self.assertEqual(result, {"checked": 2, "removed": 1, "reclassified": 1, "duplicates": 0})
            rows = conn.execute("SELECT name,event_type,status FROM source_items").fetchall()
            self.assertEqual([(row["name"], row["event_type"]) for row in rows], [("样例新游", "launch")])
            self.assertEqual(rows[0]["status"], "10:00 正式上线")
            conn.close()

    def test_233_launch_date_uses_detail_online_time_and_merges_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            detail = {"onlineTime": "2026-08-21 09:00:00", "appName": "诡秘之主"}
            upsert_items(conn, [{
                "source": "233_leyuan", "source_item_id": "1503016",
                "name": "诡秘之主", "event_type": "launch", "event_time": "2026-08-21",
                "status": "旧上线卡片", "raw": {"banner": {"id": 1}, "detail": detail},
            }], "2026-08-22T06:00:00+08:00")
            upsert_items(conn, [{
                "source": "233_leyuan", "source_item_id": "1503016",
                "name": "诡秘之主", "event_type": "launch", "event_time": "2026-08-24",
                "status": "下载送乐币", "raw": {"banner": {"id": 2}, "detail": detail},
            }], "2026-08-25T06:00:00+08:00")

            result = repair_233_launch_dates(conn)

            self.assertEqual(result, {
                "checked": 2, "corrected": 0, "reclassified": 0, "duplicates": 1,
            })
            rows = conn.execute(
                "SELECT event_time,status,last_seen_at,raw_json FROM source_items"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_time"], "2026-08-21")
            self.assertEqual(rows[0]["status"], "下载送乐币")
            self.assertEqual(rows[0]["last_seen_at"], "2026-08-25T06:00:00+08:00")
            self.assertEqual(json.loads(rows[0]["raw_json"])["banner"]["id"], 2)
            conn.close()

    def test_233_launch_date_ignores_distant_original_online_time(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            upsert_items(conn, [{
                "source": "233_leyuan", "source_item_id": "old-game",
                "name": "老游戏", "event_type": "launch", "event_time": "2026-08-24",
                "raw": {"detail": {"onlineTime": "2023-11-03 10:00:00"}},
            }], "2026-08-25T06:00:00+08:00")

            result = repair_233_launch_dates(conn)

            self.assertEqual(result, {
                "checked": 1, "corrected": 0, "reclassified": 0, "duplicates": 0,
            })
            event_time = conn.execute("SELECT event_time FROM source_items").fetchone()[0]
            self.assertEqual(event_time, "2026-08-24")
            conn.close()

    def test_233_schedule_announcement_is_removed_from_launch_semantics(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            upsert_items(conn, [{
                "source": "233_leyuan", "source_item_id": "915955",
                "name": "王者万象棋", "event_type": "launch", "event_time": "2026-08-27",
                "status": "王者万象棋",
                "raw": {
                    "banner": {
                        "name": "王者万象棋",
                        "_config": {"content": "将于8月27日晚19点公布正式上线时间！"},
                    },
                    "detail": {"onlineTime": "2030-01-01 10:00:00"},
                },
            }], "2026-08-25T06:00:00+08:00")

            result = repair_233_launch_dates(conn)

            self.assertEqual(result, {
                "checked": 1, "corrected": 0, "reclassified": 1, "duplicates": 0,
            })
            row = conn.execute("SELECT event_type,event_time,status FROM source_items").fetchone()
            self.assertEqual((row["event_type"], row["event_time"]), ("announcement", "2026-08-27"))
            self.assertEqual(row["status"], "将于8月27日晚19点公布正式上线时间！")
            conn.close()


if __name__ == "__main__":
    unittest.main()
