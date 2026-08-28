"""第二阶段模型迁移、双读审计和快照回滚工具。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import connect
from newgame_monitor.phase2_model import audit_phase2_model


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _checked_db(path: Path, *, must_exist: bool = True) -> Path:
    resolved = path.expanduser().resolve()
    if must_exist and (not resolved.is_file() or resolved.stat().st_size == 0):
        raise ValueError(f"数据库文件不存在或为空：{resolved}")
    if resolved.suffix.casefold() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError(f"拒绝操作非 SQLite 后缀文件：{resolved}")
    return resolved


def _sqlite_snapshot(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        raise FileExistsError(f"快照已存在：{target_path}")
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"快照 quick_check 失败：{result}")
    finally:
        target.close()
        source.close()


def apply_migration(db_path: Path, backup_dir: Path) -> dict:
    db_path = _checked_db(db_path)
    migration_id = f"phase2-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    snapshot = backup_dir.expanduser().resolve() / f"{migration_id}.db"
    _sqlite_snapshot(db_path, snapshot)
    conn = connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO model_migration_runs(
              migration_id,model_version,started_at,status,snapshot_path
            ) VALUES (?,2,?,'running',?)
            """,
            (migration_id, _now(), str(snapshot)),
        )
        conn.commit()
        rebuild_catalog(conn)
        audit = audit_phase2_model(conn)
        if audit["status"] != "ok":
            raise RuntimeError("第二阶段双读审计不一致：" + ",".join(audit["issues"]))
        conn.execute(
            """
            UPDATE model_migration_runs SET finished_at=?,status='success',audit_json=?
            WHERE migration_id=?
            """,
            (
                _now(), json.dumps(audit, ensure_ascii=False, separators=(",", ":")),
                migration_id,
            ),
        )
        conn.commit()
        return {
            "status": "success", "migration_id": migration_id,
            "snapshot": str(snapshot), "audit": audit,
        }
    except Exception as exc:
        conn.rollback()
        try:
            conn.execute(
                """
                UPDATE model_migration_runs SET finished_at=?,status='failed',error=?
                WHERE migration_id=?
                """,
                (_now(), str(exc)[:1000], migration_id),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def audit_migration(db_path: Path) -> dict:
    conn = connect(_checked_db(db_path))
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        audit = audit_phase2_model(conn)
        audit["quick_check"] = result
        if result != "ok":
            audit["status"] = "mismatch"
            audit["issues"].append("sqlite_quick_check")
        return audit
    finally:
        conn.close()


def rollback_migration(db_path: Path, snapshot_path: Path) -> dict:
    db_path = _checked_db(db_path)
    snapshot_path = _checked_db(snapshot_path)
    current_backup = db_path.with_name(
        f"{db_path.stem}.before-rollback-{datetime.now().strftime('%Y%m%dT%H%M%S')}{db_path.suffix}"
    )
    _sqlite_snapshot(db_path, current_backup)
    restore_tmp = db_path.with_name(f".{db_path.name}.{uuid.uuid4().hex}.restore")
    source = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(restore_tmp)
    try:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"回滚快照 quick_check 失败：{result}")
    finally:
        target.close()
        source.close()
    os.replace(restore_tmp, db_path)
    return {
        "status": "rolled_back", "database": str(db_path),
        "restored_snapshot": str(snapshot_path), "pre_rollback_backup": str(current_backup),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    apply_parser = sub.add_parser("apply", help="创建一致快照并应用第二阶段模型")
    apply_parser.add_argument("--db", type=Path, required=True)
    apply_parser.add_argument("--backup-dir", type=Path, required=True)
    audit_parser = sub.add_parser("audit", help="执行新旧模型双读一致性检查")
    audit_parser.add_argument("--db", type=Path, required=True)
    rollback_parser = sub.add_parser("rollback", help="使用指定一致快照回滚数据库")
    rollback_parser.add_argument("--db", type=Path, required=True)
    rollback_parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "apply":
        result = apply_migration(args.db, args.backup_dir)
    elif args.command == "audit":
        result = audit_migration(args.db)
    else:
        result = rollback_migration(args.db, args.snapshot)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"success", "ok", "rolled_back"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
