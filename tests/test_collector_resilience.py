import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from newgame_monitor import app_cache_collectors, collectors


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload or {}
        self.status_code = status
        self.content = json.dumps(self.payload, ensure_ascii=False).encode()

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class CollectorResilienceTest(unittest.TestCase):
    def test_http_retry_recovers_from_transient_405(self):
        with patch.object(
            collectors.requests,
            "get",
            side_effect=[FakeResponse(status=405), FakeResponse({"ok": True})],
        ) as mocked, patch.object(collectors.time, "sleep"):
            response = collectors._get("https://example.test/retry")
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(mocked.call_count, 2)

    def test_vivo_follows_has_next_until_last_page(self):
        launch_1 = {
            "data": {
                "hasNext": True,
                "listData": [{"id": 1, "name": "Vivo 首页", "pkgName": "vivo.one"}],
            }
        }
        launch_2 = {
            "data": {
                "hasNext": False,
                "listData": [{"id": 2, "name": "Vivo 末页", "pkgName": "vivo.two"}],
            }
        }
        beta = {
            "data": {
                "hasNext": False,
                "limitedTestGameList": [{"id": 3, "name": "Vivo 测试", "pkgName": "vivo.beta"}],
            }
        }
        with patch.object(
            collectors, "_get",
            side_effect=[FakeResponse(launch_1), FakeResponse(launch_2), FakeResponse(beta)],
        ) as mocked:
            items, raws, metrics = collectors.collect_vivo()
        self.assertEqual(len(items), 3)
        self.assertEqual(len(raws), 3)
        self.assertTrue(metrics["complete"])
        self.assertEqual(metrics["coverage_status"], "complete")
        self.assertEqual(metrics["endpoints"]["launch"]["pages"], 2)
        self.assertEqual(mocked.call_args_list[1].kwargs["params"]["pageIndex"], 2)

    def test_adb_retries_timeout_with_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            adb = Path(temporary) / "adb.exe"
            adb.touch()
            with patch.object(app_cache_collectors, "_adb_path", return_value=adb), patch.object(
                app_cache_collectors.subprocess,
                "check_output",
                side_effect=[subprocess.TimeoutExpired("adb", 45), b"device"],
            ) as mocked, patch.object(app_cache_collectors.time, "sleep"):
                output = app_cache_collectors._adb("get-state")
        self.assertEqual(output, b"device")
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(mocked.call_args.kwargs["timeout"], 45)

    def test_vertical_ui_marks_max_swipe_as_truncated(self):
        counter = {"value": 0}

        def parser(_xml):
            counter["value"] += 1
            value = counter["value"]
            return [{
                "source_item_id": str(value), "event_type": "launch",
                "event_time": "2026-08-28", "name": f"游戏{value}",
            }]

        with patch.object(app_cache_collectors, "_dump_ui", return_value=b"<hierarchy />"), patch.object(
            app_cache_collectors, "_adb", return_value=b""
        ), patch.object(app_cache_collectors.time, "sleep"):
            items, _raws, metrics = app_cache_collectors._collect_vertical_ui(
                "contract-test", parser, max_swipes=2,
            )
        self.assertEqual(len(items), 2)
        self.assertFalse(metrics["complete"])
        self.assertEqual(metrics["stop_reason"], "max_swipes")
        self.assertEqual(metrics["coverage_status"], "truncated")

    def test_huawei_selects_newest_cache_and_reports_age(self):
        old = json.dumps({
            "rtnCode": 0, "name": "新游",
            "games": [{"appId": "old", "name": "旧缓存", "pkgName": "old.pkg"}],
        }, ensure_ascii=False).encode()
        new = json.dumps({
            "rtnCode": 0, "name": "新游",
            "games": [{"appId": "new", "name": "新缓存", "pkgName": "new.pkg"}],
        }, ensure_ascii=False).encode()

        def adb_side_effect(*args):
            joined = " ".join(args)
            if "find " in joined:
                return b"/cache/old\n/cache/new\n"
            if "stat" in joined and "/cache/old" in joined:
                return b"100\n"
            if "stat" in joined and "/cache/new" in joined:
                return b"200\n"
            return b""

        with patch.object(
            app_cache_collectors, "_refresh_huawei_new_games",
            return_value=(None, {
                "refresh_requested": False, "refresh_started_epoch": 200,
                "refresh_succeeded": False,
            }),
        ), patch.object(app_cache_collectors, "_adb", side_effect=adb_side_effect), patch.object(
            app_cache_collectors, "_read_root_file",
            side_effect=lambda path: old if path.endswith("old") else new,
        ), patch.object(app_cache_collectors.time, "time", return_value=250):
            items, _raws, metrics = app_cache_collectors.collect_huawei_cache()
        self.assertEqual([item["name"] for item in items], ["新缓存"])
        self.assertEqual(metrics["cache_age_seconds"], 50)
        self.assertTrue(metrics["complete"])
        self.assertEqual(metrics["coverage_status"], "complete")


if __name__ == "__main__":
    unittest.main()
