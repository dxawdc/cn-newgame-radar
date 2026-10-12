import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from newgame_monitor.import_job import read_status, run_import, save_status, validate_run_id


class ImportJobTest(unittest.TestCase):
    def test_run_persists_receipt_without_inline_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('newgame_monitor.import_job.import_bundle', return_value={'duplicate': True}) as importer:
                result = run_import('20260916-071635', root)
            self.assertEqual(result['state'], 'success')
            self.assertFalse(importer.call_args.kwargs['cache_icons'])
            self.assertEqual(read_status(root / 'import-jobs', '20260916-071635'), result)

    def test_failure_preserves_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('newgame_monitor.import_job.import_bundle', side_effect=ValueError('校验失败')):
                with self.assertRaises(ValueError):
                    run_import('20260916-071635', root)
            result = read_status(root / 'import-jobs', '20260916-071635')
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['error'], '校验失败')

    def test_failure_retains_last_import_stage(self):
        def fail_import(*args, **kwargs):
            kwargs['progress']('rebuild_catalog')
            raise RuntimeError('目录失败')

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('newgame_monitor.import_job.import_bundle', side_effect=fail_import):
                with self.assertRaises(RuntimeError):
                    run_import('20261009-071859', root)
            result = read_status(root / 'import-jobs', '20261009-071859')
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['stage'], 'rebuild_catalog')
            self.assertIn('updated_at', result)

    def test_oom_does_not_leave_running_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_status(root, '20260916-071635', {'state': 'running'})
            probe = CompletedProcess([], 0, 'ActiveState=failed\nResult=oom-kill\n', '')
            with patch('newgame_monitor.import_job.subprocess.run', return_value=probe):
                result = read_status(root, '20260916-071635')
            self.assertEqual(result['state'], 'failed')
            self.assertIn('oom-kill', result['error'])

    def test_invalid_run_id_rejected(self):
        for value in ['../../etc', '20260916;reboot', '']:
            with self.assertRaises(ValueError):
                validate_run_id(value)
