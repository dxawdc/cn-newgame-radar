"""在服务启动或发布前独立执行数据库 Schema migration。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from newgame_monitor.db import migrate_database


def _snapshot(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"迁移前快照校验失败：{result}")
    finally:
        target.close()
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="独立应用新游监控数据库 Schema")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()

    db_path = args.db.expanduser().resolve()
    snapshot = None
    if db_path.exists() and db_path.stat().st_size and args.backup_dir:
        backup_dir = args.backup_dir.expanduser().resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        snapshot = backup_dir / f"pre-schema-{timestamp}.db"
        _snapshot(db_path, snapshot)

    version = migrate_database(db_path)
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    if quick_check != "ok":
        raise RuntimeError(f"迁移后数据库校验失败：{quick_check}")
    print(json.dumps({
        "status": "ok",
        "database": str(db_path),
        "schema_version": version,
        "snapshot": str(snapshot) if snapshot else None,
        "quick_check": quick_check,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
