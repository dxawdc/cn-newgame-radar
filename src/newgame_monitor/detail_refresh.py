"""按产品增量补齐详情；子进程限时，历史补齐不参与每日发布门槛。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .db import connect, _apply_schema_migrations
from .gallery import extract_gallery_urls

PUBLIC_SOURCES = {
    'taptap', 'vivo_gamecenter', 'haoyou_kuaibao', 'uc_9game',
    'huawei_gamecenter', 'xiaomi_gamecenter', '4399_gamebox',
}
LOCAL_SOURCES = {'honor_gamecenter', 'oppo_gamecenter'}
DETAIL_FIELDS = (
    'name', 'package_name', 'developer', 'category', 'tags_json', 'gameplay_intro',
    'full_description', 'icon_url', 'detail_url', 'version_name', 'raw_json',
)


def raw_object(value: str | dict | None) -> dict:
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def listing_fingerprint(item: dict) -> str:
    # 时间、热度、评分与观测时间不触发详情重抓；事件日期照常由采集器更新。
    fields = ('name', 'package_name', 'version_name', 'detail_url', 'icon_url',
              'gameplay_intro', 'full_description')
    data = {key: item.get(key) or '' for key in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare_items(conn, items: list[dict]) -> list[dict]:
    """把列表指纹及既有重试状态带到新事件行，避免同产品换日期后重新排队。"""
    result = []
    for item in items:
        previous = conn.execute(
            'SELECT raw_json FROM source_items WHERE source=? AND source_item_id=? '
            'ORDER BY last_seen_at DESC,id DESC LIMIT 1',
            (item['source'], item['source_item_id']),
        ).fetchone()
        previous_raw = raw_object(previous['raw_json']) if previous else {}
        raw = dict(item.get('raw') or {})
        # 内部状态只能来自数据库，不能接受渠道载荷伪造的状态。
        raw.pop('_detail_refresh', None)
        state = previous_raw.get('_detail_refresh')
        if isinstance(state, dict):
            raw['_detail_refresh'] = state
        elif previous_raw.get('_listing_detail_fingerprint'):
            raw['_detail_refresh'] = {'fingerprint': previous_raw['_listing_detail_fingerprint'],
                                      'status': 'unattempted'}
        raw['_listing_detail_fingerprint'] = listing_fingerprint(item)
        raw['_previous_listing_detail_fingerprint'] = previous_raw.get('_listing_detail_fingerprint', '')
        result.append({**item, 'raw': raw})
    return result


def _time(value: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(value or '')
        return value if value.tzinfo else value.astimezone()
    except (ValueError, TypeError):
        return None


def candidates(conn, *, since: str | None = None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now().astimezone()
    sources = PUBLIC_SOURCES | (LOCAL_SOURCES if since else set())
    selected = {}
    # 只读必要字段，逐行解析；同渠道同产品的多个事件只发起一次请求。
    rows = conn.execute(
        'SELECT id,source,source_item_id,name,full_description,icon_url,developer,'
        'event_type,last_seen_at,first_seen_at,raw_json FROM source_items WHERE id IN ('
        'SELECT id FROM (SELECT id,ROW_NUMBER() OVER ('
        'PARTITION BY source,source_item_id ORDER BY last_seen_at DESC,id DESC) AS position '
        'FROM source_items) WHERE position=1)'
    )
    seen = set()
    for row in rows:
        key = (row['source'], row['source_item_id'])
        if key in seen or row['source'] not in sources:
            continue
        seen.add(key)
        if since and row['last_seen_at'] < since:
            continue
        raw = raw_object(row['raw_json'])
        state = raw.get('_detail_refresh') or {}
        if not isinstance(state, dict):
            state = {}
        fingerprint = raw.get('_listing_detail_fingerprint') or ''
        previous = state.get('fingerprint') or raw.get('_previous_listing_detail_fingerprint')
        changed = bool(fingerprint and previous and fingerprint != previous)
        missing = not (str(row['full_description'] or '').strip()
                       and row['icon_url'] and row['developer']
                       and extract_gallery_urls(row['source'], raw))
        retry_after = _time(state.get('retry_after'))
        if not changed and retry_after and retry_after > now:
            continue
        checked = _time(state.get('checked_at'))
        last_seen = _time(row['last_seen_at'])
        active = row['event_type'] in {'reservation', 'appointment', 'beta', 'test'}
        stale = (not since and active and last_seen and last_seen > now - timedelta(days=30)
                 and (not checked or checked < now - timedelta(days=7)))
        if not (changed or missing or stale):
            continue
        reason = 'changed' if changed else ('missing' if missing else 'stale')
        priority = 0 if since and row['first_seen_at'] >= since else (1 if changed else 2)
        selected[key] = {'source': key[0], 'source_item_id': key[1], 'reason': reason,
                         'fingerprint': fingerprint, 'priority': priority,
                         'last_attempt': state.get('checked_at', '')}
    return sorted(selected.values(), key=lambda item: (item['priority'], item['last_attempt'],
                                                       item['source'], item['source_item_id']))


def _stamp(conn, candidate: dict, status: str, hours: int, error: str = '') -> None:
    now = datetime.now().astimezone()
    state = {'fingerprint': candidate['fingerprint'], 'status': status,
             'checked_at': now.isoformat(), 'retry_after': (now + timedelta(hours=hours)).isoformat()}
    if error:
        state['error'] = error[:500]
    for row in conn.execute('SELECT id,raw_json FROM source_items WHERE source=? AND source_item_id=?',
                            (candidate['source'], candidate['source_item_id'])).fetchall():
        raw = raw_object(row['raw_json'])
        raw['_detail_refresh'] = state
        conn.execute('UPDATE source_items SET raw_json=? WHERE id=?',
                     (json.dumps(raw, ensure_ascii=False, separators=(',', ':')), row['id']))
    conn.commit()


def refresh_one(db: Path, source: str, source_item_id: str, raw_dir: Path, force: bool = False) -> dict:
    """详情函数只看到这个产品的内存副本，不可能展开成全库联网补齐。"""
    from . import enrichment as e
    target = connect(db, migrate=False)
    scratch = sqlite3.connect(':memory:')
    scratch.row_factory = sqlite3.Row
    _apply_schema_migrations(scratch)
    try:
        originals = target.execute('SELECT * FROM source_items WHERE source=? AND source_item_id=?',
                                    (source, source_item_id)).fetchall()
        if not originals:
            return {'updated': 0, 'status': 'missing'}
        columns = list(originals[0].keys())
        scratch.executemany(f"INSERT INTO source_items ({','.join(columns)}) VALUES "
                            f"({','.join('?' for _ in columns)})", [tuple(row) for row in originals])
        scratch.commit()
        if force:
            # 仅清空内存副本的缺口判断字段以触发刷新，绝不清空生产数据。
            scratch.execute("UPDATE source_items SET full_description=NULL")
            if source == 'uc_9game':
                for row in scratch.execute('SELECT id,raw_json FROM source_items').fetchall():
                    raw = raw_object(row['raw_json'])
                    raw.pop('detail', None)
                    scratch.execute('UPDATE source_items SET raw_json=? WHERE id=?', (json.dumps(raw), row['id']))
            scratch.commit()
        operations = {
            'taptap': lambda: e.enrich_taptap_descriptions(scratch, delay=0),
            'vivo_gamecenter': lambda: e.enrich_vivo_public_details(scratch),
            'haoyou_kuaibao': lambda: e.enrich_haoyou_details(scratch, delay=0),
            'uc_9game': lambda: e.enrich_9game_screenshots(scratch, raw_dir, delay=0),
            'huawei_gamecenter': lambda: e.enrich_huawei_public_details(scratch, delay=0),
            'xiaomi_gamecenter': lambda: e.enrich_xiaomi_public_details(scratch, delay=0),
            '4399_gamebox': lambda: e.enrich_missing_4399(scratch),
            'honor_gamecenter': lambda: e.enrich_honor_cache_metadata(scratch),
            'oppo_gamecenter': lambda: e.enrich_oppo_offline_media(scratch),
        }
        detail = operations[source]()
        e.backfill_full_descriptions(scratch)
        merged = conflicts = 0
        for original in originals:
            refreshed = scratch.execute('SELECT * FROM source_items WHERE id=?', (original['id'],)).fetchone()
            if refreshed is None:
                continue
            values = {}
            for field in DETAIL_FIELDS:
                # 新接口缺字段/请求失败，不得抹掉已有介绍、媒体或标识。
                values[field] = refreshed[field] if refreshed[field] not in (None, '', '[]') else original[field]
            raw = raw_object(original['raw_json'])
            raw.update(raw_object(refreshed['raw_json']))
            values['raw_json'] = json.dumps(raw, ensure_ascii=False, separators=(',', ':'))
            if all(values[field] == original[field] for field in DETAIL_FIELDS):
                continue
            result = target.execute(
                f"UPDATE source_items SET {','.join(field + '=?' for field in DETAIL_FIELDS)} "
                'WHERE id=? AND updated_at=? AND raw_json=?',
                (*values.values(), original['id'], original['updated_at'], original['raw_json']),
            )
            merged += result.rowcount
            conflicts += not result.rowcount
        target.commit()
        return {**detail, 'merged': merged, 'conflicts': conflicts}
    finally:
        scratch.close()
        target.close()


def run_refresh(db: Path, raw_dir: Path, *, since: str | None = None,
                budget_seconds: float = 120, max_products: int = 20) -> dict:
    deadline = math.inf if budget_seconds <= 0 else time.monotonic() + budget_seconds
    conn = connect(db, migrate=False)
    try:
        queue = candidates(conn, since=since)
        result = {'eligible': len(queue), 'attempted': 0, 'completed': 0, 'failed': 0,
                  'timed_out': 0, 'deferred': 0}
        selected = queue if max_products <= 0 else queue[:max_products]
        for candidate in selected:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                break
            _stamp(conn, candidate, 'running', 24)
            command = [sys.executable, '-m', 'newgame_monitor.detail_refresh', 'one',
                       '--db', str(db), '--raw-dir', str(raw_dir),
                       '--source', candidate['source'], '--source-item-id', candidate['source_item_id']]
            if candidate['reason'] in {'changed', 'stale'}:
                command.append('--force')
            result['attempted'] += 1
            print(f"详情补齐 {result['attempted']}/{len(selected)} "
                  f"{candidate['source']}:{candidate['source_item_id']} ({candidate['reason']})", flush=True)
            try:
                completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                           errors='replace', timeout=min(30, remaining),
                                           env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
                if completed.returncode:
                    raise RuntimeError(completed.stderr[-500:])
                detail = json.loads(completed.stdout)
                if detail.get('failed') or detail.get('conflicts'):
                    raise RuntimeError(json.dumps(detail, ensure_ascii=False))
                _stamp(conn, candidate, 'success', 168)
                result['completed'] += 1
            except subprocess.TimeoutExpired:
                # subprocess.run 已结束并回收此专用子进程；未提交事务由 SQLite 回滚。
                _stamp(conn, candidate, 'timeout', 24, '单产品或本轮详情时间预算耗尽')
                result['timed_out'] += 1
                break
            except Exception as exc:
                _stamp(conn, candidate, 'failed', 24, str(exc))
                result['failed'] += 1
        result['deferred'] = len(queue) - result['attempted']
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['one', 'history'])
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--raw-dir', type=Path, default=Path('raw'))
    parser.add_argument('--source', choices=sorted(PUBLIC_SOURCES | LOCAL_SOURCES))
    parser.add_argument('--source-item-id')
    parser.add_argument('--since', help='只处理指定时间后观察到的产品')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--budget-seconds', type=float, default=1800,
                        help='总预算；0 表示不设总时限（仍受单产品超时约束）')
    parser.add_argument('--max-products', type=int, default=0,
                        help='最多处理产品数；0 表示处理全部候选')
    args = parser.parse_args()
    if args.command == 'one':
        if not args.source or not args.source_item_id:
            parser.error('单产品任务必须指定来源和产品编号')
        result = refresh_one(args.db, args.source, args.source_item_id, args.raw_dir, args.force)
    else:
        result = run_refresh(args.db, args.raw_dir, since=args.since,
                             budget_seconds=args.budget_seconds,
                             max_products=args.max_products)
        from .catalog import rebuild_catalog
        conn = connect(args.db, migrate=False)
        try:
            result['games'] = rebuild_catalog(conn)
        finally:
            conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
