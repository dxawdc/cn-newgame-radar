import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from newgame_monitor.db import connect, upsert_items
from newgame_monitor.simulator_sync import export_bundle, import_bundle


class SimulatorSyncTest(unittest.TestCase):
    def test_export_and_import_incremental_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_db = root / "source.db"
            source_raw = root / "source-raw"
            source_icons = root / "source-icons"
            bundle = root / "bundle.tar.gz"
            observed = "2026-08-21T07:15:00+08:00"
            conn = connect(source_db)
            upsert_items(
                conn,
                [{
                    "source": "honor_gamecenter",
                    "source_item_id": "honor-1",
                    "name": "同步测试游戏",
                    "developer": "测试厂商",
                    "tags": ["角色扮演"],
                    "gameplay_intro": "测试介绍",
                    "icon_url": "local-icon://ui/honor_gamecenter/honor-1.webp",
                    "event_type": "beta",
                    "event_time": "2026-08-22",
                    "raw": {"evidence": "ui"},
                }, {
                    "source": "taptap",
                    "source_item_id": "tap-sync-1",
                    "name": "TapTap 同步测试游戏",
                    "tags": ["休闲"],
                    "icon_url": "https://example.com/tap.webp",
                    "event_type": "reservation",
                    "event_time": "2026-08-23",
                    "raw": {"detail_screenshots": [{"original_url": "https://example.com/1.webp"}]},
                }],
                observed,
            )
            for source in ("taptap", "huawei-cache", "honor-ui", "oppo-ui"):
                conn.execute(
                    """
                    INSERT INTO collection_runs(source, started_at, finished_at, status, item_count)
                    VALUES (?, ?, ?, 'success', 1)
                    """,
                    (source, observed, observed),
                )
            conn.commit()
            conn.close()
            icon = source_icons / "ui" / "honor_gamecenter" / "honor-1.webp"
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b"fake-webp")
            raw = source_raw / "2026-08-21" / "honor-ui" / "071500-list.raw"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"ui-evidence")

            result = export_bundle(
                source_db, source_raw, source_icons, bundle,
                "2026-08-21T07:00:00+08:00",
            )
            self.assertEqual(result["items"], 2)
            self.assertEqual(result["icons"], 1)

            target_db = root / "target.db"
            target_raw = root / "target-raw"
            target_icons = root / "target-icons"
            target = connect(target_db)
            upsert_items(
                target,
                [{
                    "source": "taptap", "source_item_id": "tap-1",
                    "name": "已有公开渠道游戏", "tags": [],
                    "event_type": "launch", "event_time": "2026-08-20", "raw": {},
                }],
                observed,
            )
            target.close()
            imported = import_bundle(bundle, target_db, target_raw, target_icons, cache_icons=False)
            self.assertEqual(imported["items"], 2)
            self.assertEqual(imported["runs"], 4)
            self.assertTrue((target_icons / "ui" / "honor_gamecenter" / "honor-1.webp").is_file())
            self.assertTrue((target_raw / "2026-08-21" / "honor-ui" / "071500-list.raw").is_file())
            target = connect(target_db)
            self.assertEqual(target.execute("SELECT COUNT(*) FROM source_items").fetchone()[0], 3)
            self.assertEqual(
                target.execute(
                    "SELECT developer FROM source_items WHERE source='honor_gamecenter'"
                ).fetchone()[0],
                "测试厂商",
            )
            target.close()

    def test_import_rejects_parent_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "unsafe.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                payload = json.dumps({"schema_version": 1}).encode()
                manifest = tarfile.TarInfo("manifest.json")
                manifest.size = len(payload)
                archive.addfile(manifest, io.BytesIO(payload))
                bad = tarfile.TarInfo("../outside.txt")
                bad.size = 3
                archive.addfile(bad, io.BytesIO(b"bad"))
            with self.assertRaisesRegex(ValueError, "不安全路径"):
                import_bundle(bundle, root / "db.sqlite", root / "raw", root / "icons")


if __name__ == "__main__":
    unittest.main()
