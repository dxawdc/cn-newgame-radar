import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from newgame_monitor import cli, detail_refresh as dr
from newgame_monitor.db import connect, upsert_items


class DetailRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.db'
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        self.now = datetime.now().astimezone()
        self.since = self.now.isoformat()

    def add(self, item_id='one', *, complete=False, observed=None, version='1'):
        item = {'source': 'taptap', 'source_item_id': item_id, 'name': '测试产品' + item_id,
                'event_type': 'reservation', 'event_time': '2026-12-01',
                'detail_url': 'https://www.taptap.cn/app/' + item_id, 'version_name': version,
                'raw': {}}
        if complete:
            item.update(full_description='已有介绍', developer='测试开发商', icon_url='https://example.test/icon.png',
                        raw={'detail_screenshots': ['https://example.test/gallery.png']})
        upsert_items(self.conn, dr.prepare_items(self.conn, [item]), observed or self.since)

    def test_daily_excludes_history_and_complete_unchanged(self):
        self.add('old', observed=(self.now - timedelta(days=10)).isoformat())
        self.add('complete', complete=True)
        self.add('new')
        queue = dr.candidates(self.conn, since=self.since)
        self.assertEqual([x['source_item_id'] for x in queue], ['new'])

    def test_changes_survive_deferral_and_bypass_old_backoff(self):
        self.add('one', complete=True)
        self.add('one', complete=True, version='2')
        self.add('one', complete=True, version='2')
        queue = dr.candidates(self.conn, since=self.since)
        self.assertEqual(queue[0]['reason'], 'changed')
        dr._stamp(self.conn, queue[0], 'failed', 24)
        self.assertEqual(dr.candidates(self.conn, since=self.since), [])
        self.add('one', complete=True, version='3')
        self.assertEqual(dr.candidates(self.conn, since=self.since)[0]['reason'], 'changed')

    def test_missing_retry_and_history_refresh(self):
        self.add('one')
        queue = dr.candidates(self.conn)
        dr._stamp(self.conn, queue[0], 'failed', 24)
        self.assertEqual(dr.candidates(self.conn), [])
        self.assertEqual(len(dr.candidates(self.conn, now=self.now + timedelta(days=2))), 1)
        self.add('complete', complete=True)
        self.assertIn('complete', [x['source_item_id'] for x in dr.candidates(self.conn)])
        self.conn.execute("UPDATE source_items SET event_type='launch' WHERE source_item_id='complete'")
        self.conn.commit()
        self.assertNotIn('complete', [x['source_item_id'] for x in dr.candidates(self.conn)])

    def test_worker_sees_only_selected_product_and_preserves_existing_on_failure(self):
        self.add('one', complete=True)
        self.add('other')
        original = dict(self.conn.execute("SELECT * FROM source_items WHERE source_item_id='one'").fetchone())

        def enrich(scratch, delay):
            self.assertEqual(scratch.execute('SELECT COUNT(*) FROM source_items').fetchone()[0], 1)
            self.assertEqual(scratch.execute('SELECT source_item_id FROM source_items').fetchone()[0], 'one')
            return {'failed': 1}

        with patch('newgame_monitor.enrichment.enrich_taptap_descriptions', side_effect=enrich):
            result = dr.refresh_one(self.db, 'taptap', 'one', Path(self.temp.name), force=True)
        current = dict(self.conn.execute("SELECT * FROM source_items WHERE source_item_id='one'").fetchone())
        self.assertEqual(current['full_description'], original['full_description'])
        self.assertEqual(current['icon_url'], original['icon_url'])
        self.assertEqual(result['failed'], 1)

    def test_changed_description_can_be_shorter_and_other_products_untouched(self):
        self.add('one', complete=True)
        self.add('other')
        before = dict(self.conn.execute("SELECT * FROM source_items WHERE source_item_id='other'").fetchone())

        def enrich(scratch, delay):
            scratch.execute("UPDATE source_items SET full_description='新介绍'")
            scratch.commit()
            return {'updated': 1}

        with patch('newgame_monitor.enrichment.enrich_taptap_descriptions', side_effect=enrich):
            result = dr.refresh_one(self.db, 'taptap', 'one', Path(self.temp.name), force=True)
        self.assertEqual(result['merged'], 1)
        self.assertEqual(self.conn.execute("SELECT full_description FROM source_items WHERE source_item_id='one'").fetchone()[0], '新介绍')
        self.assertEqual(dict(self.conn.execute("SELECT * FROM source_items WHERE source_item_id='other'").fetchone()), before)

    def test_budget_zero_and_timeout_defer_without_blocking(self):
        self.add('one')
        self.add('two')
        with patch.object(dr.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 0.1)) as run:
            result = dr.run_refresh(self.db, Path(self.temp.name), budget_seconds=0)
            run.assert_called_once()
            self.assertEqual(result['timed_out'], 1)
        with patch.object(dr.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 0.1)):
            result = dr.run_refresh(self.db, Path(self.temp.name), budget_seconds=0.5)
            self.assertEqual(result['attempted'], 0)
            result = dr.run_refresh(self.db, Path(self.temp.name), budget_seconds=5)
        self.assertEqual(result['timed_out'], 1)
        self.assertEqual(result['deferred'], 0)

    def test_daily_cli_does_not_download_media_or_scan_all_history(self):
        self.add('history', observed=(self.now - timedelta(days=10)).isoformat())
        args = ['cli', '--sources', 'taptap', '--db', str(self.db),
                '--raw-dir', str(Path(self.temp.name) / 'raw'), '--detail-budget-seconds', '0']
        item = {'source': 'taptap', 'source_item_id': 'today', 'name': '今日产品',
                'event_type': 'reservation', 'event_time': '2026-12-01', 'raw': {}}
        with patch.object(sys, 'argv', args), patch.dict(cli.COLLECTORS, {'taptap': lambda: ([item], [])}), \
                patch('newgame_monitor.icon_cache.cache_remote_icons') as icons, \
                patch('newgame_monitor.gallery.cache_remote_screenshots') as screenshots, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
            icons.assert_not_called()
            screenshots.assert_not_called()
        stage = self.conn.execute("SELECT detail_json FROM pipeline_stages WHERE stage='incremental_details'").fetchone()
        self.assertEqual(json.loads(stage[0])['eligible'], 1)

    def test_concurrent_update_is_not_overwritten(self):
        self.add('one', complete=True)

        def enrich(scratch, delay):
            self.conn.execute("UPDATE source_items SET full_description='并发更新的介绍',raw_json='{}'")
            self.conn.commit()
            scratch.execute("UPDATE source_items SET full_description='过期副本'")
            scratch.commit()
            return {'updated': 1}

        with patch('newgame_monitor.enrichment.enrich_taptap_descriptions', side_effect=enrich):
            result = dr.refresh_one(self.db, 'taptap', 'one', Path(self.temp.name), force=True)
        self.assertEqual(result['conflicts'], 1)
        self.assertEqual(self.conn.execute('SELECT full_description FROM source_items').fetchone()[0], '并发更新的介绍')

    def test_real_worker_process_and_product_limit(self):
        # 不提供公网详情 URL，验证真实子进程、中文编码和数据库合并，不发网络请求。
        for item_id in ('one', 'two'):
            upsert_items(self.conn, [{'source': 'uc_9game', 'source_item_id': item_id,
                                    'name': '进程测试', 'event_type': 'launch', 'raw': {}}], self.since)
        result = dr.run_refresh(self.db, Path(self.temp.name), since=self.since,
                                budget_seconds=10, max_products=1)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['deferred'], 1)

    def test_history_since_is_supported(self):
        self.add('old', observed=(self.now - timedelta(days=2)).isoformat())
        self.add('new', observed=self.since)
        result = dr.run_refresh(self.db, Path(self.temp.name), since=self.since,
                                budget_seconds=0.1, max_products=0)
        self.assertGreaterEqual(result['eligible'], 1)


if __name__ == '__main__':
    unittest.main()
