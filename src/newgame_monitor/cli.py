import argparse
import json
import os
import statistics
import sys
import uuid
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
    pipeline_id = str(uuid.uuid4())
    conn = connect(args.db)
    conn.execute(
        "INSERT INTO pipeline_runs(run_id,started_at,status) VALUES (?,?,?)",
        (pipeline_id, observed.isoformat(), "running"),
    )
    conn.commit()
    if "oppo-ui" in args.sources and not args.ui_details:
        # 日常任务只打开尚缺“完整介绍 + 图集”的产品；已补齐产品不重复进详情。
        os.environ["NEWGAME_OPPO_COMPLETE_DETAILS"] = json.dumps(
            _oppo_complete_detail_names(conn), ensure_ascii=False,
        )
    summary = {}

    def begin_stage(stage: str, source: str = "") -> str:
        started_at = datetime.now().astimezone().isoformat()
        conn.execute(
            """
            INSERT INTO pipeline_stages(run_id,stage,source,started_at,status)
            VALUES (?,?,?,?, 'running')
            ON CONFLICT(run_id,stage,source) DO UPDATE SET
              started_at=excluded.started_at,finished_at=NULL,status='running',detail_json='{}'
            """,
            (pipeline_id, stage, source, started_at),
        )
        conn.commit()
        return started_at

    def finish_stage(stage: str, status: str, detail: dict, source: str = "") -> None:
        conn.execute(
            """
            UPDATE pipeline_stages
            SET finished_at=?,status=?,detail_json=?
            WHERE run_id=? AND stage=? AND source=?
            """,
            (
                datetime.now().astimezone().isoformat(), status,
                json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                pipeline_id, stage, source,
            ),
        )
        conn.commit()

    for source in args.sources:
        begin_stage("collected", source)
        started = datetime.now().astimezone().isoformat()
        run_id = conn.execute(
            """
            INSERT INTO collection_runs(run_group_id,source,started_at,status)
            VALUES (?, ?, ?, ?)
            """,
            (pipeline_id, source, started, "running"),
        ).lastrowid
        conn.commit()
        try:
            collector_result = COLLECTORS[source]()
            items, raw_responses = collector_result[:2]
            metrics = collector_result[2] if len(collector_result) > 2 else {}
            raw_path = args.raw_dir / observed.strftime("%Y-%m-%d") / source
            raw_path.mkdir(parents=True, exist_ok=True)
            for label, content in raw_responses:
                (raw_path / f"{observed.strftime('%H%M%S')}-{label}.raw").write_bytes(content)
            count = upsert_items(conn, items, observed.isoformat())
            baselines = [
                int(row["item_count"])
                for row in conn.execute(
                    """
                    SELECT item_count FROM collection_runs
                    WHERE source=? AND status IN ('success','degraded') AND item_count>0 AND id<>?
                    ORDER BY id DESC LIMIT 7
                    """,
                    (source, run_id),
                )
            ]
            if len(baselines) >= 3:
                baseline = float(statistics.median(baselines))
                metrics["recent_count_median"] = baseline
                metrics["count_ratio"] = round(count / baseline, 3) if baseline else None
                if baseline >= 5 and count < baseline * 0.4:
                    metrics["complete"] = False
                    metrics["count_anomaly"] = "below_40_percent_of_recent_median"
            run_status = "degraded" if metrics.get("complete") is False else "success"
            conn.execute(
                """
                UPDATE collection_runs
                SET finished_at=?, status=?, item_count=?, metrics_json=? WHERE id=?
                """,
                (
                    datetime.now().astimezone().isoformat(), run_status, count,
                    json.dumps(metrics, ensure_ascii=False, separators=(",", ":")), run_id,
                ),
            )
            conn.commit()
            summary[source] = {"status": run_status, "items": count, "metrics": metrics}
            finish_stage("collected", run_status, summary[source], source)
        except Exception as exc:
            conn.execute(
                "UPDATE collection_runs SET finished_at=?, status='failed', error=? WHERE id=?",
                (datetime.now().astimezone().isoformat(), str(exc)[:1000], run_id),
            )
            conn.commit()
            summary[source] = {"status": "failed", "error": str(exc)}
            finish_stage("collected", "failed", summary[source], source)

    stages = [
        ("quality", lambda: prune_legacy_haoyou_timeline(conn)),
        ("233_event_dates", lambda: repair_233_launch_dates(conn)),
        ("enrichment", lambda: enrich_missing_4399(conn)),
        ("descriptions", lambda: backfill_full_descriptions(conn)),
        ("taptap_details", lambda: enrich_taptap_descriptions(conn)),
        ("vivo_public_details", lambda: enrich_vivo_public_details(conn)),
        ("haoyou_details", lambda: enrich_haoyou_details(conn)),
        ("9game_screenshots", lambda: enrich_9game_screenshots(conn, args.raw_dir)),
        ("huawei_public_details", lambda: enrich_huawei_public_details(conn)),
        ("xiaomi_public_details", lambda: enrich_xiaomi_public_details(conn)),
    ]
    if "honor-ui" in args.sources:
        stages.append(("honor_cache_metadata", lambda: enrich_honor_cache_metadata(conn)))
    if "oppo-ui" in args.sources:
        stages.append(("oppo_offline_media", lambda: enrich_oppo_offline_media(conn)))
        # 历史 /sdcard 快照不得在日常采集中冒充本轮详情。
        # 只有显式逐项回填时才读取；正常路径使用 ResourceDto 原图。
        if args.ui_details:
            stages.append(("oppo_ui_snapshots", lambda: enrich_oppo_ui_snapshots(conn)))
    stages.append(("name_fallback", lambda: enrich_name_lookup_fallback(conn)))

    for stage, operation in stages:
        begin_stage(stage)
        try:
            detail = operation() or {}
            summary[stage] = {"status": "success", **detail}
            finish_stage(stage, "success", summary[stage])
        except Exception as exc:
            summary[stage] = {"status": "failed", "error": str(exc)}
            finish_stage(stage, "failed", summary[stage])

    begin_stage("catalog")
    try:
        summary["catalog"] = {"status": "success", "games": rebuild_catalog(conn)}
        finish_stage("catalog", "success", summary["catalog"])
    except Exception as exc:
        summary["catalog"] = {"status": "failed", "error": str(exc)}
        finish_stage("catalog", "failed", summary["catalog"])

    if summary["catalog"]["status"] == "success":
        final_stages = [
            ("completeness", lambda: audit_catalog_completeness(conn)),
            ("icons", lambda: cache_remote_icons(conn, args.icon_dir)),
            ("screenshots", lambda: cache_remote_screenshots(conn, args.screenshot_dir)),
        ]
        for stage, operation in final_stages:
            begin_stage(stage)
            try:
                detail = operation() or {}
                summary[stage] = {"status": "success", **detail}
                finish_stage(stage, "success", summary[stage])
            except Exception as exc:
                summary[stage] = {"status": "failed", "error": str(exc)}
                finish_stage(stage, "failed", summary[stage])

    collector_statuses = [summary[source]["status"] for source in args.sources]
    publishable = any(status in {"success", "degraded"} for status in collector_statuses)
    catalog_ok = summary["catalog"]["status"] == "success"
    has_degradation = any(
        isinstance(value, dict) and value.get("status") in {"failed", "degraded"}
        for value in summary.values()
    )
    pipeline_status = "failed" if not (publishable and catalog_ok) else (
        "partial" if has_degradation else "success"
    )
    finished_at = datetime.now().astimezone().isoformat()
    pipeline_summary = {
        "run_id": pipeline_id,
        "status": pipeline_status,
        "publishable": publishable and catalog_ok,
    }
    summary["pipeline"] = pipeline_summary
    conn.execute(
        """
        UPDATE pipeline_runs
        SET finished_at=?,status=?,summary_json=? WHERE run_id=?
        """,
        (
            finished_at, pipeline_status,
            json.dumps(pipeline_summary, ensure_ascii=False, separators=(",", ":")),
            pipeline_id,
        ),
    )
    conn.commit()
    conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # 部分失败由同步层按渠道隔离发布；仅“没有任何可发布渠道”或目录失败时阻断。
    return 0 if publishable and catalog_ok else 1


if __name__ == "__main__":
    sys.exit(main())
