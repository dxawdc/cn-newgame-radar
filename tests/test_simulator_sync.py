import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newgame_monitor.db import connect, upsert_items
from newgame_monitor.simulator_sync import export_bundle, import_bundle


class SimulatorSyncTest(unittest.TestCase):
    @staticmethod
    def _record_run(conn, source, observed, status="success", error=None):
        conn.execute(
            """
            INSERT INTO collection_runs(source, started_at, finished_at, status, item_count, error)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (source, observed, observed, status, error),
        )

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
                    "raw": {
                        "evidence": "ui",
                        "ui_detail": {"screenshot_urls": [
                            "local-screenshot://gallery/oppo_gamecenter/gallery-1.webp"
                        ]},
                    },
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
            gallery = source_icons / "gallery" / "oppo_gamecenter" / "gallery-1.webp"
            gallery.parent.mkdir(parents=True)
            gallery.write_bytes(b"fake-gallery-webp")
            raw = source_raw / "2026-08-21" / "honor-ui" / "071500-list.raw"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"ui-evidence")

            result = export_bundle(
                source_db, source_raw, source_icons, bundle,
                "2026-08-21T07:00:00+08:00",
            )
            self.assertEqual(result["items"], 2)
            self.assertEqual(result["icons"], 2)

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
            self.assertTrue(
                (target_icons / "gallery" / "oppo_gamecenter" / "gallery-1.webp").is_file()
            )
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

    def test_partial_bundle_publishes_successful_source_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_db = root / "base.db"
            conn = connect(base_db)
            observed = "2026-08-28T07:00:00+08:00"
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "oppo-old",
                "name": "OPPO 保留产品", "event_type": "launch",
                "event_time": "2026-08-28", "raw": {},
            }], observed)
            conn.close()

            work_db = root / "work.db"
            target_db = root / "target.db"
            shutil.copy2(base_db, work_db)
            shutil.copy2(base_db, target_db)
            conn = connect(work_db)
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "tap-new",
                "name": "TapTap 新产品", "event_type": "reservation",
                "event_time": "2026-09-01", "raw": {},
            }], observed)
            self._record_run(conn, "taptap", observed)
            self._record_run(conn, "oppo-ui", observed, "failed", "ADB 超时")
            conn.commit()
            conn.close()

            bundle = root / "partial.tar.gz"
            exported = export_bundle(
                work_db, root / "raw", root / "icons", bundle, observed, base_db,
            )
            self.assertEqual(exported["publish_status"], "partial")
            self.assertEqual(exported["published_sources"], ["taptap"])
            self.assertEqual(exported["items"], 1)

            imported = import_bundle(
                bundle, target_db, root / "target-raw", root / "target-icons",
                cache_icons=False,
            )
            self.assertEqual(imported["publish_status"], "partial")
            conn = connect(target_db)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_items WHERE source='taptap'").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_items WHERE source='oppo_gamecenter'").fetchone()[0],
                1,
            )
            conn.close()

    def test_snapshot_diff_syncs_historical_update_delete_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_db = root / "base.db"
            observed = "2026-08-28T07:00:00+08:00"
            conn = connect(base_db)
            upsert_items(conn, [{
                "source": "honor_gamecenter", "source_item_id": "honor-update",
                "name": "历史详情待补全", "event_type": "beta",
                "event_time": "2026-08-01", "gameplay_intro": "旧介绍", "raw": {},
            }, {
                "source": "honor_gamecenter", "source_item_id": "honor-delete",
                "name": "已删除产品", "event_type": "launch",
                "event_time": "2026-08-02", "raw": {},
            }], "2026-08-01T07:00:00+08:00")
            conn.close()

            work_db = root / "work.db"
            target_db = root / "target.db"
            shutil.copy2(base_db, work_db)
            shutil.copy2(base_db, target_db)
            conn = connect(work_db)
            conn.execute(
                "UPDATE source_items SET full_description=? WHERE source_item_id='honor-update'",
                ("补全后的历史产品详情" * 30,),
            )
            conn.execute("DELETE FROM source_items WHERE source_item_id='honor-delete'")
            self._record_run(conn, "honor-ui", observed)
            conn.commit()
            conn.close()

            bundle = root / "diff.tar.gz"
            exported = export_bundle(
                work_db, root / "raw", root / "icons", bundle, observed, base_db,
            )
            self.assertEqual(exported["items"], 1)
            self.assertEqual(exported["tombstones"], 1)

            first = import_bundle(
                bundle, target_db, root / "target-raw", root / "target-icons",
                cache_icons=False,
            )
            second = import_bundle(
                bundle, target_db, root / "target-raw", root / "target-icons",
                cache_icons=False,
            )
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            conn = connect(target_db)
            rows = list(conn.execute("SELECT source_item_id,full_description FROM source_items"))
            self.assertEqual([row["source_item_id"] for row in rows], ["honor-update"])
            self.assertTrue(rows[0]["full_description"].startswith("补全后"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applied_bundles").fetchone()[0], 1)
            conn.close()

    def test_catalog_failure_rolls_back_database_and_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_db = root / "source.db"
            observed = "2026-08-28T07:00:00+08:00"
            conn = connect(source_db)
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "rollback-item",
                "name": "回滚测试产品", "event_type": "launch",
                "event_time": "2026-08-28",
                "icon_url": "local-icon://ui/oppo_gamecenter/rollback.webp",
                "raw": {},
            }], observed)
            self._record_run(conn, "oppo-ui", observed)
            conn.commit()
            conn.close()
            icon = root / "icons" / "ui" / "oppo_gamecenter" / "rollback.webp"
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b"rollback-media")
            bundle = root / "rollback.tar.gz"
            export_bundle(source_db, root / "raw", root / "icons", bundle, observed)

            target_db = root / "target.db"
            target_icons = root / "target-icons"
            with patch("newgame_monitor.simulator_sync.rebuild_catalog", side_effect=RuntimeError("故障注入")):
                with self.assertRaisesRegex(RuntimeError, "故障注入"):
                    import_bundle(
                        bundle, target_db, root / "target-raw", target_icons,
                        cache_icons=False,
                    )
            conn = connect(target_db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applied_bundles").fetchone()[0], 0)
            conn.close()
            self.assertFalse((target_icons / "ui" / "oppo_gamecenter" / "rollback.webp").exists())

    def test_media_promotion_failure_removes_partial_file_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_db = root / "source.db"
            observed = "2026-08-28T07:00:00+08:00"
            conn = connect(source_db)
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "media-failure",
                "name": "媒体回滚产品", "event_type": "launch",
                "event_time": "2026-08-28",
                "icon_url": "local-icon://ui/oppo_gamecenter/media-failure.webp",
                "raw": {},
            }], observed)
            self._record_run(conn, "oppo-ui", observed)
            conn.commit()
            conn.close()
            icon = root / "icons" / "ui" / "oppo_gamecenter" / "media-failure.webp"
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b"partial-media")
            bundle = root / "media-failure.tar.gz"
            export_bundle(source_db, root / "raw", root / "icons", bundle, observed)

            target_db = root / "target.db"
            target_icons = root / "target-icons"
            with patch("newgame_monitor.simulator_sync.os.fsync", side_effect=OSError("磁盘写入失败")):
                with self.assertRaisesRegex(OSError, "磁盘写入失败"):
                    import_bundle(
                        bundle, target_db, root / "target-raw", target_icons,
                        cache_icons=False,
                    )
            conn = connect(target_db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applied_bundles").fetchone()[0], 0)
            conn.close()
            self.assertFalse(
                (target_icons / "ui" / "oppo_gamecenter" / "media-failure.webp").exists()
            )

    def test_existing_different_media_isolated_without_blocking_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_db = root / "source.db"
            observed = "2026-08-28T07:00:00+08:00"
            conn = connect(source_db)
            upsert_items(conn, [{
                "source": "honor_gamecenter", "source_item_id": "media-conflict",
                "name": "媒体冲突产品", "event_type": "launch",
                "event_time": "2026-08-28",
                "icon_url": "local-icon://ui/honor_gamecenter/media-conflict.webp",
                "raw": {},
            }], observed)
            self._record_run(conn, "honor-ui", observed)
            conn.commit()
            conn.close()
            icon = root / "icons" / "ui" / "honor_gamecenter" / "media-conflict.webp"
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b"new-media")
            bundle = root / "media-conflict.tar.gz"
            export_bundle(source_db, root / "raw", root / "icons", bundle, observed)

            target_db = root / "target.db"
            target_icons = root / "target-icons"
            existing = target_icons / "ui" / "honor_gamecenter" / "media-conflict.webp"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"existing-media")
            imported = import_bundle(
                bundle, target_db, root / "target-raw", target_icons,
                cache_icons=False,
            )
            self.assertEqual(imported["items"], 1)
            self.assertEqual(len(imported["media_conflicts"]), 1)
            self.assertEqual(existing.read_bytes(), b"existing-media")

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
