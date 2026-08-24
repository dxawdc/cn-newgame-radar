"""只读审计各渠道事件文案中的明确日期是否与入库日期一致。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path


DATE_PATTERN = re.compile(
    r"(?:(20\d{2})[年./-])?(\d{1,2})[月./-](\d{1,2})日?"
)
START_AFTER = re.compile(
    r"^\s*(?:\d{1,2}:\d{2}\s*)?"
    r"(?:首发|正式上线|上线|开测|开启(?:测试|预约|招募)|预下载|"
    r"公测|内测|首测|终测|限量测试|删档测试|不删档测试|发售)"
)
START_BEFORE = re.compile(
    r"(?:首发|正式上线|上线|开测|预下载|公测|内测|首测|终测|"
    r"限量测试|删档测试|不删档测试|发售)(?:时间|日期)?[：:]?\s*$"
)
END_AFTER = re.compile(r"^\s*(?:截止|结束|关闭|到期)")
END_BEFORE = re.compile(r"(?:截至|截止|结束于|持续至|招募至|报名至)[：:]?\s*$")


def _date_for_match(match: re.Match[str], observed_at: str) -> str:
    observed = datetime.fromisoformat(observed_at).date()
    year_text, month_text, day_text = match.groups()
    month, day = int(month_text), int(day_text)
    year = int(year_text) if year_text else observed.year + (
        1 if month < observed.month - 6 else -1 if month > observed.month + 6 else 0
    )
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def explicit_event_dates(text: str, observed_at: str) -> list[dict[str, str]]:
    """返回文案中的明确开始/结束日期；无法判断用途的日期标为 unknown。"""
    results = []
    for match in DATE_PATTERN.finditer(text or ""):
        before = (text or "")[max(0, match.start() - 18):match.start()]
        after = (text or "")[match.end():match.end() + 20]
        kind = "unknown"
        if END_AFTER.match(after) or END_BEFORE.search(before):
            kind = "end"
        if START_AFTER.match(after) or START_BEFORE.search(before):
            kind = "start"
        value = _date_for_match(match, observed_at)
        if value:
            results.append({
                "date": value,
                "kind": kind,
                "context": f"{before}[{match.group(0)}]{after}".strip(),
            })
    return results


def audit(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        """
        SELECT id,source,source_item_id,name,event_type,event_time,event_end_time,
               status,gameplay_intro,first_seen_at
        FROM source_items
        ORDER BY source,id
        """
    ))
    totals = Counter(row["source"] for row in rows)
    blanks = Counter(row["source"] for row in rows if not row["event_time"])
    start_mentions = Counter()
    mismatches = []
    end_mismatches = []
    for row in rows:
        texts = []
        for field in ("status", "gameplay_intro"):
            value = (row[field] or "").strip()
            if value and value not in texts:
                texts.append(value)
        mentions = []
        for text in texts:
            mentions.extend(explicit_event_dates(text, row["first_seen_at"]))
        starts = sorted({x["date"] for x in mentions if x["kind"] == "start"})
        ends = sorted({x["date"] for x in mentions if x["kind"] == "end"})
        if starts:
            start_mentions[row["source"]] += 1
        event_date = (row["event_time"] or "")[:10]
        if starts and event_date not in starts:
            mismatches.append({
                "id": row["id"], "source": row["source"], "name": row["name"],
                "event_type": row["event_type"], "stored": event_date,
                "expected": starts, "status": row["status"],
                "first_seen": row["first_seen_at"][:10],
                "high_risk": not event_date or event_date == row["first_seen_at"][:10],
                "evidence": [x["context"] for x in mentions if x["kind"] == "start"],
            })
        end_date = (row["event_end_time"] or "")[:10]
        if ends and end_date and end_date not in ends:
            end_mismatches.append({
                "id": row["id"], "source": row["source"], "name": row["name"],
                "stored": end_date, "expected": ends, "status": row["status"],
            })

    duplicates = []
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["source"], row["source_item_id"], row["event_type"]), []).append(row)
    for (source, source_item_id, event_type), members in groups.items():
        dates = sorted({(row["event_time"] or "")[:10] for row in members})
        if len(dates) > 1:
            duplicates.append({
                "source": source, "source_item_id": source_item_id,
                "event_type": event_type, "dates": dates,
                "names": sorted({row["name"] for row in members}),
                "row_ids": [row["id"] for row in members],
                "statuses": sorted({row["status"] or "" for row in members}),
            })
    conn.close()
    return {
        "database": str(db_path),
        "rows": len(rows),
        "sources": {
            source: {
                "rows": totals[source],
                "blank_event_time": blanks[source],
                "explicit_start_mentions": start_mentions[source],
            }
            for source in sorted(totals)
        },
        "start_date_mismatches": mismatches,
        "end_date_mismatches": end_mismatches,
        "same_item_multiple_dates": duplicates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/newgame_monitor.db"))
    args = parser.parse_args()
    print(json.dumps(audit(args.db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
