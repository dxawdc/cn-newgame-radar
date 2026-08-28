import tempfile
import unittest
from pathlib import Path

from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import connect, upsert_items
from scripts.migrate_phase2 import apply_migration, rollback_migration


class Phase2MigrationTest(unittest.TestCase):
    def test_apply_audit_snapshot_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "test.db"
            conn = connect(db_path)
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "migration-1",
                "name": "迁移样例", "event_type": "launch",
                "event_time": "2026-08-28", "raw": {},
            }], "2026-08-28T08:00:00+08:00")
            rebuild_catalog(conn)
            original_uuid = conn.execute(
                "SELECT game_uuid FROM canonical_games"
            ).fetchone()[0]
            conn.close()

            result = apply_migration(db_path, root / "backups")
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["audit"]["status"], "ok")
            snapshot = Path(result["snapshot"])
            self.assertTrue(snapshot.is_file())

            conn = connect(db_path)
            conn.execute("UPDATE source_items SET name='迁移后临时修改'")
            conn.commit()
            conn.close()
            rolled_back = rollback_migration(db_path, snapshot)
            self.assertEqual(rolled_back["status"], "rolled_back")
            conn = connect(db_path)
            row = conn.execute(
                "SELECT name,game_uuid FROM canonical_games"
            ).fetchone()
            quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
            conn.close()
            self.assertEqual(row["name"], "迁移样例")
            self.assertEqual(row["game_uuid"], original_uuid)
            self.assertEqual(quick_check, "ok")


if __name__ == "__main__":
    unittest.main()
