import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import connect, upsert_items
from newgame_monitor.phase2_model import (
    audit_phase2_model,
    claim_review_jobs,
    finish_review_job,
    resolve_game_uuid,
)


class Phase2ModelTest(unittest.TestCase):
    def test_uuid_survives_rename_and_legacy_id_redirects(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-28T08:00:00+08:00"
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "rename-1",
                "name": "远星计划（限时开测）", "developer": "远星工作室",
                "event_type": "beta", "event_time": "2026-09-01", "raw": {},
            }], observed)
            rebuild_catalog(conn)
            before = conn.execute(
                "SELECT id,game_uuid FROM canonical_games"
            ).fetchone()
            conn.execute("UPDATE source_items SET name='远星计划'")
            conn.commit()
            rebuild_catalog(conn)
            after = conn.execute(
                "SELECT id,game_uuid FROM canonical_games"
            ).fetchone()
            self.assertEqual(after["game_uuid"], before["game_uuid"])
            self.assertEqual(
                resolve_game_uuid(conn, before["game_uuid"]), after["game_uuid"],
            )
            if after["id"] != before["id"]:
                redirect = conn.execute(
                    "SELECT new_game_id FROM canonical_game_id_redirects WHERE old_game_id=?",
                    (before["id"],),
                ).fetchone()
                self.assertEqual(redirect[0], after["id"])
            conn.close()

    def test_parallel_model_keeps_schedule_history_and_unknown_events(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            base = {
                "source": "taptap", "source_item_id": "schedule-1",
                "name": "改期样例", "package_name": "com.example.schedule",
                "developer": "样例工作室", "event_type": "beta",
                "status": "删档测试", "raw": {},
            }
            upsert_items(conn, [{**base, "event_time": "2026-09-01"}], "2026-08-20T08:00:00+08:00")
            upsert_items(conn, [{**base, "event_time": "2026-09-08"}], "2026-08-28T08:00:00+08:00")
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "unknown-1",
                "name": "未知事件样例", "event_type": "mystery_drop",
                "event_time": "2026-09-03", "raw": {},
            }], "2026-08-28T08:00:00+08:00")
            rebuild_catalog(conn)
            event = conn.execute(
                """
                SELECT re.id,re.controlled_event_type
                FROM release_events re
                JOIN platform_listings pl ON pl.id=re.listing_id
                WHERE pl.source_item_id='schedule-1'
                """
            ).fetchone()
            self.assertEqual(event["controlled_event_type"], "test")
            versions = list(conn.execute(
                "SELECT scheduled_at FROM event_schedule_versions WHERE release_event_id=? ORDER BY scheduled_at",
                (event["id"],),
            ))
            self.assertEqual([row[0] for row in versions], ["2026-09-01", "2026-09-08"])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM test_rounds").fetchone()[0], 1,
            )
            quarantine = conn.execute(
                "SELECT raw_event_type,status FROM event_type_quarantine"
            ).fetchone()
            self.assertEqual(tuple(quarantine), ("mystery_drop", "pending"))
            self.assertEqual(audit_phase2_model(conn)["status"], "ok")
            conn.close()

    def test_missing_detail_gallery_jobs_are_traceable_and_retryable(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "missing-1",
                "name": "资料缺口样例", "event_type": "launch",
                "event_time": "2026-08-28", "raw": {},
            }], "2026-08-28T08:00:00+08:00")
            rebuild_catalog(conn)
            jobs = claim_review_jobs(conn, "detail")
            self.assertEqual(len(jobs), 1)
            row = conn.execute(
                "SELECT status,retry_count,evidence_json FROM review_queue WHERE id=?",
                (jobs[0]["id"],),
            ).fetchone()
            self.assertEqual((row["status"], row["retry_count"]), ("processing", 1))
            self.assertEqual(json.loads(row["evidence_json"])["source"], "oppo_gamecenter")
            finish_review_job(
                conn, jobs[0]["id"], status="retry", result={"error": "temporary"},
                next_retry_at="2026-08-29T08:00:00+08:00",
            )
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT status FROM review_queue WHERE id=?", (jobs[0]["id"],)).fetchone()[0],
                "retry",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM review_queue WHERE queue_type='gallery'"
                ).fetchone()[0],
                1,
            )
            conn.close()

    def test_name_variant_is_candidate_not_direct_identity_decision(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-28T08:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "oppo_gamecenter", "source_item_id": "candidate-a",
                    "name": "星河远征", "developer": "甲工作室",
                    "event_type": "launch", "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "taptap", "source_item_id": "candidate-b",
                    "name": "星河远征-科幻冒险手游", "developer": "乙工作室",
                    "event_type": "reservation", "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            rebuild_catalog(conn)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM canonical_games").fetchone()[0], 2,
            )
            candidate = conn.execute(
                "SELECT score,status FROM identity_candidates"
            ).fetchone()
            self.assertIsNotNone(candidate)
            self.assertLess(candidate["score"], 0.85)
            self.assertEqual(candidate["status"], "pending")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM review_queue WHERE queue_type='identity' AND status='pending'"
                ).fetchone()[0],
                1,
            )
            conn.close()

    def test_exact_same_name_with_conflicting_identity_is_not_merged(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-28T08:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "same-name-a",
                    "name": "重名游戏", "package_name": "com.alpha.game",
                    "developer": "甲方工作室", "event_type": "launch",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "same-name-b",
                    "name": "重名游戏", "package_name": "com.beta.game",
                    "developer": "乙方工作室", "event_type": "launch",
                    "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            rebuild_catalog(conn)
            games = list(conn.execute(
                "SELECT game_uuid,canonical_key,name FROM canonical_games ORDER BY canonical_key"
            ))
            self.assertEqual(len(games), 2)
            self.assertTrue(all("#identity:" in row["canonical_key"] for row in games))
            candidate = conn.execute(
                "SELECT score,status FROM identity_candidates"
            ).fetchone()
            self.assertIsNotNone(candidate)
            self.assertLess(candidate["score"], 0.85)
            self.assertEqual(candidate["status"], "pending")
            conn.close()

    def test_legacy_same_name_collision_enters_split_review(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "test.db")
            observed = "2026-08-28T08:00:00+08:00"
            upsert_items(conn, [
                {
                    "source": "taptap", "source_item_id": "legacy-a",
                    "name": "历史重名", "event_type": "launch",
                    "event_time": "2026-09-01", "raw": {},
                },
                {
                    "source": "oppo_gamecenter", "source_item_id": "legacy-b",
                    "name": "历史重名", "event_type": "launch",
                    "event_time": "2026-09-02", "raw": {},
                },
            ], observed)
            rebuild_catalog(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM canonical_games").fetchone()[0], 1)
            conn.execute(
                "UPDATE source_items SET package_name='com.alpha.game',developer='甲工作室' WHERE source_item_id='legacy-a'"
            )
            conn.execute(
                "UPDATE source_items SET package_name='com.beta.game',developer='乙工作室' WHERE source_item_id='legacy-b'"
            )
            conn.commit()
            rebuild_catalog(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM canonical_games").fetchone()[0], 1)
            review = conn.execute(
                "SELECT priority,evidence_json FROM review_queue WHERE queue_key LIKE 'identity:split:%'"
            ).fetchone()
            self.assertIsNotNone(review)
            self.assertEqual(review["priority"], 95)
            self.assertEqual(
                json.loads(review["evidence_json"])["reason"],
                "conflicting_package_and_developer",
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
