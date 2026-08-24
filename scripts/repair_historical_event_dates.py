"""修复已由原始证据确认的 OPPO/荣耀历史日期解析错误。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from audit_oppo_timeline_raw import audit as audit_oppo_raw
from newgame_monitor.app_cache_collectors import _oppo_explicit_date, _xiaomi_event_time
from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.collectors import _parse_4399_event_date
from newgame_monitor.db import connect, upsert_items


ITEM_FIELDS = (
    "source", "source_item_id", "name", "package_name", "developer", "category",
    "gameplay_intro", "full_description", "icon_url", "detail_url", "rating",
    "version_name", "size_bytes", "event_type", "event_time", "event_end_time",
    "canonical_key", "status",
)


def _item_from_row(row: sqlite3.Row, expected_date: str) -> dict:
    item = {field: row[field] for field in ITEM_FIELDS}
    item["event_time"] = expected_date
    item["tags"] = json.loads(row["tags_json"] or "[]")
    raw = json.loads(row["raw_json"] or "{}")
    raw["event_date"] = expected_date
    raw["date_repair"] = {
        "reason": "replayed_raw_timeline",
        "previous_event_time": row["event_time"],
        "repaired_at": datetime.now().astimezone().isoformat(),
    }
    item["raw"] = raw
    return item


def build_plan(conn: sqlite3.Connection, raw_dir: Path, db_path: Path) -> dict:
    oppo = audit_oppo_raw(raw_dir, db_path)["database_mismatches"]
    honor = []
    for row in conn.execute(
        """
        SELECT id,name,event_type,event_time,status,gameplay_intro,first_seen_at
        FROM source_items
        WHERE source='honor_gamecenter' AND event_time=''
        ORDER BY id
        """
    ):
        expected = _oppo_explicit_date(
            row["gameplay_intro"], datetime.fromisoformat(row["first_seen_at"])
        )
        if expected:
            honor.append({
                "id": row["id"], "name": row["name"],
                "stored": row["event_time"], "expected": expected,
                "event_type": "launch" if "上线" in (row["gameplay_intro"] or "") else row["event_type"],
                "status": row["gameplay_intro"],
            })
    gamebox_4399 = []
    for row in conn.execute(
        """
        SELECT id,name,event_type,event_time,status,raw_json,first_seen_at
        FROM source_items WHERE source='4399_gamebox' ORDER BY id
        """
    ):
        raw = json.loads(row["raw_json"] or "{}")
        original = raw.get("event_date_text") or row["event_time"]
        expected = _parse_4399_event_date(
            original, datetime.fromisoformat(row["first_seen_at"])
        )
        if expected and row["event_time"] != expected:
            gamebox_4399.append({
                "action": "normalize",
                "id": row["id"], "name": row["name"],
                "stored": row["event_time"], "expected": expected,
                "event_type": row["event_type"], "status": row["status"],
            })
        elif row["event_type"] == "beta" and re.fullmatch(r"\d{1,2}-\d{1,2}", original or ""):
            gamebox_4399.append({
                "action": "remove_ambiguous_year",
                "id": row["id"], "name": row["name"],
                "stored": row["event_time"], "source_date_text": original,
                "event_type": row["event_type"], "status": row["status"],
            })
        elif row["event_time"] and not expected:
            gamebox_4399.append({
                "action": "clear_unresolved",
                "id": row["id"], "name": row["name"],
                "stored": row["event_time"], "source_date_text": original,
                "event_type": row["event_type"], "status": row["status"],
            })
    xiaomi = []
    for row in conn.execute(
        "SELECT id,name,event_type,event_time,status,raw_json FROM source_items "
        "WHERE source='xiaomi_gamecenter' ORDER BY id"
    ):
        raw = json.loads(row["raw_json"] or "{}")
        detail = raw.get("dInfo") or {}
        expected = _xiaomi_event_time(detail.get("testing") or {}, detail.get("subscribe") or {})
        if row["event_time"] != expected:
            xiaomi.append({
                "id": row["id"], "name": row["name"],
                "stored": row["event_time"], "expected": expected,
                "event_type": row["event_type"], "status": row["status"],
            })
    return {"oppo": oppo, "honor": honor, "gamebox_4399": gamebox_4399, "xiaomi": xiaomi}


def apply_plan(conn: sqlite3.Connection, plan: dict) -> dict:
    merged = moved = honor_updated = gamebox_updated = gamebox_removed = gamebox_cleared = xiaomi_updated = 0
    for repair in plan["oppo"]:
        source_row = conn.execute("SELECT * FROM source_items WHERE id=?", (repair["id"],)).fetchone()
        if source_row is None:
            continue
        expected = repair["expected"][0]
        target = conn.execute(
            """
            SELECT * FROM source_items
            WHERE source=? AND source_item_id=? AND event_type=? AND event_time=? AND id<>?
            """,
            (
                source_row["source"], source_row["source_item_id"],
                source_row["event_type"], expected, source_row["id"],
            ),
        ).fetchone()
        if target is None:
            raw = json.loads(source_row["raw_json"] or "{}")
            raw["event_date"] = expected
            raw["date_repair"] = {
                "reason": "replayed_raw_timeline",
                "previous_event_time": source_row["event_time"],
                "repaired_at": datetime.now().astimezone().isoformat(),
            }
            conn.execute(
                "UPDATE source_items SET event_time=?,raw_json=? WHERE id=?",
                (expected, json.dumps(raw, ensure_ascii=False, separators=(",", ":")), source_row["id"]),
            )
            moved += 1
            continue

        observed_at = max(source_row["last_seen_at"], target["last_seen_at"])
        upsert_items(conn, [_item_from_row(source_row, expected)], observed_at)
        conn.execute(
            """
            UPDATE source_items
            SET first_seen_at=MIN(first_seen_at,?), last_seen_at=MAX(last_seen_at,?)
            WHERE id=?
            """,
            (source_row["first_seen_at"], source_row["last_seen_at"], target["id"]),
        )
        conn.execute("DELETE FROM canonical_members WHERE source_row_id=?", (source_row["id"],))
        conn.execute("DELETE FROM source_items WHERE id=?", (source_row["id"],))
        merged += 1

    for repair in plan["honor"]:
        raw_row = conn.execute("SELECT raw_json FROM source_items WHERE id=?", (repair["id"],)).fetchone()
        if raw_row is None:
            continue
        raw = json.loads(raw_row["raw_json"] or "{}")
        raw["event_date"] = repair["expected"]
        raw["date_repair"] = {
            "reason": "explicit_card_text",
            "previous_event_time": repair["stored"],
            "repaired_at": datetime.now().astimezone().isoformat(),
        }
        conn.execute(
            """
            UPDATE source_items
            SET event_time=?,event_type=?,status=?,raw_json=?
            WHERE id=?
            """,
            (
                repair["expected"], repair["event_type"], repair["status"],
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), repair["id"],
            ),
        )
        honor_updated += 1

    for repair in plan["gamebox_4399"]:
        if repair.get("action") == "remove_ambiguous_year":
            conn.execute("DELETE FROM canonical_members WHERE source_row_id=?", (repair["id"],))
            deleted = conn.execute("DELETE FROM source_items WHERE id=?", (repair["id"],)).rowcount
            gamebox_removed += deleted
            continue
        if repair.get("action") == "clear_unresolved":
            row = conn.execute("SELECT raw_json FROM source_items WHERE id=?", (repair["id"],)).fetchone()
            if row is None:
                continue
            raw = json.loads(row["raw_json"] or "{}")
            raw.setdefault("event_date_text", repair["source_date_text"])
            raw["date_repair"] = {
                "reason": "unresolved_source_date",
                "previous_event_time": repair["stored"],
                "repaired_at": datetime.now().astimezone().isoformat(),
            }
            conn.execute(
                "UPDATE source_items SET event_time='',raw_json=? WHERE id=?",
                (json.dumps(raw, ensure_ascii=False, separators=(",", ":")), repair["id"]),
            )
            gamebox_cleared += 1
            continue
        row = conn.execute("SELECT raw_json FROM source_items WHERE id=?", (repair["id"],)).fetchone()
        if row is None:
            continue
        raw = json.loads(row["raw_json"] or "{}")
        raw.setdefault("event_date_text", repair["stored"])
        raw["date_repair"] = {
            "reason": "normalized_source_date",
            "previous_event_time": repair["stored"],
            "repaired_at": datetime.now().astimezone().isoformat(),
        }
        conn.execute(
            "UPDATE source_items SET event_time=?,raw_json=? WHERE id=?",
            (
                repair["expected"],
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), repair["id"],
            ),
        )
        gamebox_updated += 1

    for repair in plan["xiaomi"]:
        row = conn.execute("SELECT raw_json FROM source_items WHERE id=?", (repair["id"],)).fetchone()
        if row is None:
            continue
        raw = json.loads(row["raw_json"] or "{}")
        raw["date_repair"] = {
            "reason": "xiaomi_visible_schedule_precision",
            "previous_event_time": repair["stored"],
            "repaired_at": datetime.now().astimezone().isoformat(),
        }
        conn.execute(
            "UPDATE source_items SET event_time=?,raw_json=? WHERE id=?",
            (
                repair["expected"],
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), repair["id"],
            ),
        )
        xiaomi_updated += 1

    conn.commit()
    games = rebuild_catalog(conn)
    return {
        "oppo_merged": merged,
        "oppo_moved": moved,
        "honor_updated": honor_updated,
        "gamebox_4399_updated": gamebox_updated,
        "gamebox_4399_removed_ambiguous": gamebox_removed,
        "gamebox_4399_cleared_unresolved": gamebox_cleared,
        "xiaomi_updated": xiaomi_updated,
        "catalog_games": games,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/newgame_monitor.db"))
    parser.add_argument("--raw-dir", type=Path, default=Path("raw"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = connect(args.db)
    plan = build_plan(conn, args.raw_dir, args.db)
    result = {"mode": "apply" if args.apply else "dry-run", "plan": plan}
    if args.apply:
        result["result"] = apply_plan(conn, plan)
    conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
