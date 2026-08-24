"""用当前解析器重放 OPPO 时间轴原始快照，核验历史事件日期。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from newgame_monitor.app_cache_collectors import _parse_oppo_timeline


FILE_PATTERN = re.compile(
    r"(?P<batch>\d{6})-(?P<section>launch|recruiting_beta|beta)-(?P<index>\d+)\.raw$"
)


def audit(raw_dir: Path, db_path: Path | None = None) -> dict:
    groups: dict[tuple[str, str, str], list[tuple[int, Path]]] = defaultdict(list)
    for path in raw_dir.glob("*/oppo-ui/*.raw"):
        match = FILE_PATTERN.fullmatch(path.name)
        if match:
            groups[(path.parent.parent.name, match["batch"], match["section"])].append(
                (int(match["index"]), path)
            )

    votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
    batch_conflicts = []
    batch_summaries = []
    for (day, batch, section), files in sorted(groups.items()):
        inherited = ""
        observed: dict[tuple[str, str], set[str]] = defaultdict(set)
        parsed_rows = 0
        for _, path in sorted(files):
            items, inherited = _parse_oppo_timeline(path.read_bytes(), section, inherited)
            parsed_rows += len(items)
            for item in items:
                observed[(item["name"], item["event_type"])].add(item["event_time"])
        for (name, event_type), dates in observed.items():
            for event_date in dates:
                votes[(event_type, name)][event_date] += 1
            if len(dates) > 1:
                batch_conflicts.append({
                    "day": day, "batch": batch, "section": section,
                    "name": name, "event_type": event_type, "dates": sorted(dates),
                })
        batch_summaries.append({
            "day": day, "batch": batch, "section": section,
            "files": len(files), "parsed_rows": parsed_rows,
            "games": len(observed),
        })

    truth = {key: set(counts) for key, counts in votes.items()}
    database_mismatches = []
    if db_path is not None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            """
            SELECT id,name,event_type,event_time,status,raw_json
            FROM source_items WHERE source='oppo_gamecenter' ORDER BY id
            """
        ):
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                raw = {}
            if "event_date" not in raw or raw.get("calendar") == "today":
                continue
            expected = truth.get((row["event_type"], row["name"]))
            if expected and row["event_time"] not in expected:
                database_mismatches.append({
                    "id": row["id"], "name": row["name"],
                    "event_type": row["event_type"], "stored": row["event_time"],
                    "expected": sorted(expected), "status": row["status"],
                })
        conn.close()

    return {
        "batches": batch_summaries,
        "within_batch_conflicts": batch_conflicts,
        "date_votes": [
            {
                "event_type": event_type, "name": name,
                "dates": dict(sorted(counts.items())),
            }
            for (event_type, name), counts in sorted(votes.items())
            if len(counts) > 1
        ],
        "database_mismatches": database_mismatches,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, default=Path("raw"))
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.raw_dir, args.db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
