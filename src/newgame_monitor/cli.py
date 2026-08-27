import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .collectors import COLLECTORS
from .catalog import audit_catalog_completeness, rebuild_catalog
from .db import connect, upsert_items
from .icon_cache import cache_remote_icons
from .gallery import cache_remote_screenshots
from .enrichment import (
    backfill_full_descriptions,
    enrich_haoyou_details,
    enrich_9game_screenshots,
    enrich_honor_cache_metadata,
    enrich_name_lookup_fallback,
    enrich_oppo_offline_media,
    enrich_oppo_ui_snapshots,
    enrich_huawei_public_details,
    enrich_missing_4399,
    enrich_taptap_descriptions,
    enrich_vivo_public_details,
    enrich_xiaomi_public_details,
)
from .event_quality import prune_legacy_haoyou_timeline, repair_233_launch_dates


def _oppo_complete_detail_names(conn) -> list[str]:
    """返回已有完整介绍和图集的 OPPO 产品归一化名称。"""
    from .catalog import normalize_game_name

    complete = set()
    rows = conn.execute(
        "SELECT name,full_description,raw_json FROM source_items "
        "WHERE source='oppo_gamecenter'"
    )
    for row in rows:
        if not (row["full_description"] or "").strip():
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        ui_detail = raw.get("ui_detail") or {}
        offline_detail = raw.get("oppo_offline_detail") or {}
        screenshots = (
            ui_detail.get("screenshot_urls")
            or offline_detail.get("screenshot_urls")
            or []
        )
        if screenshots:
            complete.add(normalize_game_name(row["name"]))
    return sorted(complete)


def main() -> int:
    parser = argparse.ArgumentParser(description="国内新游每日采集")
    parser.add_argument(
        "--sources", nargs="+",
        default=[
            "taptap", "ios-cn", "huawei-cache", "honor-ui", "xiaomi", "oppo-ui",
            "vivo", "4399", "233", "3839", "9game",
        ],
        choices=COLLECTORS,
    )
    parser.add_argument("--db", type=Path, default=Path("data/newgame_monitor.db"))
    parser.add_argument("--raw-dir", type=Path, default=Path("raw"))
    parser.add_argument("--icon-dir", type=Path, default=Path("data/icons"))
    parser.add_argument("--screenshot-dir", type=Path, default=Path("data/screenshots"))
    parser.add_argument(
        "--ui-details", action="store_true",
        help="逐项打开荣耀/OPPO 详情页，补开发商、标签和完整介绍",
    )
    args = parser.parse_args()

    if args.ui_details:
        os.environ["NEWGAME_UI_DETAILS"] = "1"

    observed = datetime.now().astimezone()
    conn = connect(args.db)
    if "oppo-ui" in args.sources and not args.ui_details:
        # 日常任务只打开尚缺“完整介绍 + 图集”的产品；已补齐产品不重复进详情。
        os.environ["NEWGAME_OPPO_COMPLETE_DETAILS"] = json.dumps(
            _oppo_complete_detail_names(conn), ensure_ascii=False,
        )
    summary = {}
    for source in args.sources:
        started = datetime.now().astimezone().isoformat()
        run_id = conn.execute(
            "INSERT INTO collection_runs(source, started_at, status) VALUES (?, ?, ?)",
            (source, started, "running"),
        ).lastrowid
        conn.commit()
        try:
            items, raw_responses = COLLECTORS[source]()
            raw_path = args.raw_dir / observed.strftime("%Y-%m-%d") / source
            raw_path.mkdir(parents=True, exist_ok=True)
            for label, content in raw_responses:
                (raw_path / f"{observed.strftime('%H%M%S')}-{label}.raw").write_bytes(content)
            count = upsert_items(conn, items, observed.isoformat())
            conn.execute(
                "UPDATE collection_runs SET finished_at=?, status='success', item_count=? WHERE id=?",
                (datetime.now().astimezone().isoformat(), count, run_id),
            )
            conn.commit()
            summary[source] = {"status": "success", "items": count}
        except Exception as exc:
            conn.execute(
                "UPDATE collection_runs SET finished_at=?, status='failed', error=? WHERE id=?",
                (datetime.now().astimezone().isoformat(), str(exc)[:1000], run_id),
            )
            conn.commit()
            summary[source] = {"status": "failed", "error": str(exc)}
    summary["quality"] = {"status": "success", **prune_legacy_haoyou_timeline(conn)}
    summary["233_event_dates"] = {"status": "success", **repair_233_launch_dates(conn)}
    summary["enrichment"] = {"status": "success", **enrich_missing_4399(conn)}
    summary["descriptions"] = {"status": "success", **backfill_full_descriptions(conn)}
    summary["taptap_details"] = {"status": "success", **enrich_taptap_descriptions(conn)}
    summary["vivo_public_details"] = {
        "status": "success", **enrich_vivo_public_details(conn)
    }
    summary["haoyou_details"] = {"status": "success", **enrich_haoyou_details(conn)}
    summary["9game_screenshots"] = {
        "status": "success", **enrich_9game_screenshots(conn, args.raw_dir)
    }
    summary["huawei_public_details"] = {
        "status": "success", **enrich_huawei_public_details(conn)
    }
    summary["xiaomi_public_details"] = {
        "status": "success", **enrich_xiaomi_public_details(conn)
    }
    if "honor-ui" in args.sources:
        summary["honor_cache_metadata"] = {
            "status": "success", **enrich_honor_cache_metadata(conn)
        }
    if "oppo-ui" in args.sources:
        summary["oppo_offline_media"] = {
            "status": "success", **enrich_oppo_offline_media(conn)
        }
        # 历史 /sdcard 快照不得在日常采集中冒充本轮详情。
        # 只有显式逐项回填时才读取；正常路径使用 ResourceDto 原图。
        if args.ui_details:
            summary["oppo_ui_snapshots"] = {
                "status": "success", **enrich_oppo_ui_snapshots(conn)
            }
    summary["name_fallback"] = {
        "status": "success", **enrich_name_lookup_fallback(conn)
    }
    summary["catalog"] = {"status": "success", "games": rebuild_catalog(conn)}
    summary["completeness"] = {
        "status": "success", **audit_catalog_completeness(conn)
    }
    summary["icons"] = {"status": "success", **cache_remote_icons(conn, args.icon_dir)}
    summary["screenshots"] = {
        "status": "success", **cache_remote_screenshots(conn, args.screenshot_dir)
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if any(v["status"] == "failed" for v in summary.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
