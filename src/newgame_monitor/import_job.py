"""由 systemd 托管增量入库，SSH 断连不影响任务及其持久回执。"""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .simulator_sync import import_bundle

STATE = Path('/var/lib/newgame-monitor')


def validate_run_id(value: str) -> str:
    if not re.fullmatch(r'\d{8}-\d{6}', value):
        raise ValueError('运行编号必须为 YYYYMMDD-HHMMSS')
    return value


def save_status(root: Path, run_id: str, result: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f'{run_id}.json.tmp'
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(root / f'{run_id}.json')


def read_status(root: Path, run_id: str) -> dict:
    path = root / f'{run_id}.json'
    result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'state': 'missing'}
    if result['state'] not in {'success', 'failed'}:
        probe = subprocess.run(
            ['systemctl', 'show', f'newgame-monitor-import@{run_id}.service',
             '--property=ActiveState,SubState,Result,ExecMainStatus'],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode:
            raise RuntimeError('无法核验远端入库服务状态')
        unit = dict(line.split('=', 1) for line in probe.stdout.splitlines() if '=' in line)
        result['unit'] = unit
        if unit.get('ActiveState') == 'failed':
            result.update(state='failed', error=f"入库服务失败：{unit.get('Result')}")
        elif unit.get('ActiveState') in {'activating', 'active', 'deactivating'}:
            result['state'] = 'running'
        elif result['state'] == 'running':
            result.update(state='failed', error='入库进程已退出但未生成完成回执，请核查日志及 bundle 回执')
    return {'run_id': run_id, **result}


def run_import(run_id: str, state: Path = STATE) -> dict:
    root = state / 'import-jobs'
    record = {'run_id': run_id, 'state': 'running', 'started_at': datetime.now().astimezone().isoformat()}
    save_status(root, run_id, record)
    def progress(stage: str) -> None:
        record.update(stage=stage, updated_at=datetime.now().astimezone().isoformat())
        save_status(root, run_id, record)
        print(json.dumps({'run_id': run_id, 'stage': stage, 'at': record['updated_at']}, ensure_ascii=False), flush=True)

    try:
        result = import_bundle(
            Path('/tmp') / f'newgame-simulator-{run_id}.tar.gz',
            state / 'data/newgame_monitor.db', state / 'raw', state / 'data/icons',
            state / 'data/screenshots', cache_icons=False, progress=progress,
        )
        record.update(state='success', result=result)
    except Exception as exc:
        record.update(state='failed', error=str(exc))
        raise
    finally:
        record['finished_at'] = datetime.now().astimezone().isoformat()
        save_status(root, run_id, record)
    return record


def media_cache(state: Path) -> dict:
    from .db import connect
    from .gallery import cache_remote_screenshots
    from .icon_cache import cache_remote_icons
    conn = connect(state / 'data/newgame_monitor.db', migrate=False)
    try:
        return {'icons': cache_remote_icons(conn, state / 'data/icons', workers=1),
                'screenshots': cache_remote_screenshots(conn, state / 'data/screenshots', workers=1)}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'status', 'media'])
    parser.add_argument('--run-id', type=validate_run_id)
    parser.add_argument('--state-dir', type=Path, default=STATE)
    args = parser.parse_args()
    if args.command != 'media' and not args.run_id:
        parser.error('必须提供 --run-id')
    if args.command == 'run':
        # 卡住时留下调用栈，便于区分锁等待、磁盘和内存压力。
        faulthandler.dump_traceback_later(120, repeat=True)
        try:
            result = run_import(args.run_id, args.state_dir)
        finally:
            faulthandler.cancel_dump_traceback_later()
    elif args.command == 'status':
        result = read_status(args.state_dir / 'import-jobs', args.run_id)
    else:
        result = media_cache(args.state_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
