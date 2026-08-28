"""第二阶段稳定身份、事件快照和复核队列的并行数据模型。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from itertools import combinations
from urllib.parse import urlsplit


CONTROLLED_EVENT_TYPES = {
    "launch": "launch",
    "announcement": "announcement",
    "reservation": "reservation",
    "pre_download": "pre_download",
    "beta": "test",
    "limited_beta": "test",
    "important_beta": "test",
    "recruiting_beta": "test_recruitment",
    "new_listing": "listing",
    "first_seen": "listing",
    "timeline": "timeline",
    "新游预约": "reservation",
    "测试招募": "test_recruitment",
    "不限量测试": "test",
    "新版本更新": "update",
    "折扣": "promotion",
}


def _normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _package_family(value: str | None) -> str:
    package = (value or "").strip().casefold()
    for suffix in (
        ".nearme.gamecenter", ".huawei", ".honor", ".vivo", ".xiaomi", ".mi", ".oppo",
    ):
        if package.endswith(suffix) and len(package) > len(suffix):
            return package[:-len(suffix)]
    return package


def _detail_identity(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return f"{parsed.hostname.casefold()}{parsed.path.rstrip('/')}"


def resolve_game_uuid(conn: sqlite3.Connection, requested_uuid: str) -> str | None:
    """解析稳定 UUID 的合并链；遇到断链、环或超长链时拒绝。"""
    current = (requested_uuid or "").strip().lower()
    if not current:
        return None
    seen: set[str] = set()
    for _ in range(17):
        if current in seen:
            return None
        seen.add(current)
        if conn.execute(
            "SELECT 1 FROM canonical_games WHERE game_uuid=?", (current,)
        ).fetchone():
            return current
        row = conn.execute(
            "SELECT new_game_uuid FROM canonical_game_uuid_redirects WHERE old_game_uuid=?",
            (current,),
        ).fetchone()
        if not row:
            return None
        current = row[0]
    return None


def _upsert_review(
    conn: sqlite3.Connection, *, queue_key: str, queue_type: str,
    game_uuid: str, listing_id: int | None, priority: int, evidence: dict,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO review_queue(
          queue_key,queue_type,game_uuid,listing_id,status,priority,
          evidence_json,first_detected_at,updated_at
        ) VALUES (?,?,?,?,'pending',?,?,?,?)
        ON CONFLICT(queue_key) DO UPDATE SET
          game_uuid=excluded.game_uuid,listing_id=excluded.listing_id,
          priority=excluded.priority,evidence_json=excluded.evidence_json,
          status=CASE
            WHEN review_queue.status IN ('resolved','dismissed') THEN review_queue.status
            ELSE 'pending' END,
          updated_at=excluded.updated_at
        """,
        (
            queue_key, queue_type, game_uuid, listing_id, priority,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), now, now,
        ),
    )


def _event_identity(listing_id: int, row: sqlite3.Row, controlled: str) -> str:
    status_key = _normalize(row["status"] or row["event_type"]) or "event"
    if controlled not in {"test", "test_recruitment"}:
        status_key = controlled
    raw = f"{listing_id}|{controlled}|{status_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _looks_actual(row: sqlite3.Row, controlled: str) -> bool:
    if controlled not in {"launch", "test"} or not row["event_time"]:
        return False
    try:
        event_day = date.fromisoformat(str(row["event_time"])[:10])
    except ValueError:
        return False
    status = row["status"] or ""
    return event_day <= date.today() and bool(re.search(r"已|开启|上线|开服|公测", status))


def _test_round_index(status: str) -> int | None:
    match = re.search(r"第([一二三四五六七八九十\d]+)次|([首二三四五六七八九十])测", status)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    if value == "首":
        return 1
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if len(value) == 1:
        return digits.get(value)
    if value.startswith("十"):
        return 10 + digits.get(value[1:], 0)
    if value.endswith("十"):
        return digits.get(value[:-1], 0) * 10
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 0) * 10 + digits.get(right, 0)
    return None


def _sync_listing_row(
    conn: sqlite3.Connection, row: sqlite3.Row, now: str,
) -> tuple[int, str]:
    game_uuid = row["game_uuid"]
    conn.execute(
        """
        INSERT INTO platform_listings(
          game_uuid,source,source_item_id,package_name,detail_url,status,
          first_seen_at,last_seen_at
        ) VALUES (?,?,?,?,?,'active',?,?)
        ON CONFLICT(source,source_item_id) DO UPDATE SET
          game_uuid=excluded.game_uuid,
          package_name=COALESCE(NULLIF(excluded.package_name,''),platform_listings.package_name),
          detail_url=COALESCE(NULLIF(excluded.detail_url,''),platform_listings.detail_url),
          status='active',
          first_seen_at=MIN(platform_listings.first_seen_at,excluded.first_seen_at),
          last_seen_at=MAX(platform_listings.last_seen_at,excluded.last_seen_at)
        """,
        (
            game_uuid, row["source"], row["source_item_id"], row["package_name"],
            row["detail_url"], row["first_seen_at"], row["last_seen_at"],
        ),
    )
    listing_id = conn.execute(
        "SELECT id FROM platform_listings WHERE source=? AND source_item_id=?",
        (row["source"], row["source_item_id"]),
    ).fetchone()[0]
    alias = unicodedata.normalize("NFKC", row["name"] or "").strip()
    conn.execute(
        """
        INSERT INTO game_aliases(
          game_uuid,alias,normalized_alias,source,first_seen_at,last_seen_at,status
        ) VALUES (?,?,?,?,?,?,'active')
        ON CONFLICT(game_uuid,normalized_alias,source) DO UPDATE SET
          alias=excluded.alias,last_seen_at=MAX(game_aliases.last_seen_at,excluded.last_seen_at),
          status='active'
        """,
        (
            game_uuid, alias, _normalize(alias), row["source"],
            row["first_seen_at"], row["last_seen_at"],
        ),
    )
    evidence = [
        ("package", row["package_name"], _normalize(row["package_name"]), 0.95),
        ("developer", row["developer"], _normalize(row["developer"]), 0.65),
        ("detail_url", row["detail_url"], _detail_identity(row["detail_url"]), 0.90),
        ("channel_id", row["source_item_id"], f"{row['source']}:{row['source_item_id']}", 1.0),
    ]
    for evidence_type, value, normalized, confidence in evidence:
        if not value or not normalized:
            continue
        conn.execute(
            """
            INSERT INTO identity_evidence(
              game_uuid,listing_id,source_row_id,evidence_type,evidence_value,
              normalized_value,confidence,observed_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(game_uuid,evidence_type,normalized_value,source_row_id)
            DO UPDATE SET observed_at=excluded.observed_at,
              evidence_value=excluded.evidence_value,confidence=excluded.confidence,
              listing_id=excluded.listing_id
            """,
            (
                game_uuid, listing_id, row["id"], evidence_type, str(value),
                normalized, confidence, row["last_seen_at"],
            ),
        )

    snapshot_payload = {
        key: row[key] for key in (
            "name", "package_name", "developer", "category", "tags_json",
            "gameplay_intro", "full_description", "icon_url", "detail_url",
            "rating", "version_name", "size_bytes", "status", "raw_json",
        )
    }
    payload_json = json.dumps(
        snapshot_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO listing_snapshots(
          listing_id,source_row_id,observed_at,payload_sha256,name,status,raw_json
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            listing_id, row["id"], row["last_seen_at"], payload_hash,
            row["name"], row["status"], payload_json,
        ),
    )
    return listing_id, game_uuid


def _sync_event(
    conn: sqlite3.Connection, row: sqlite3.Row, listing_id: int, game_uuid: str,
) -> None:
    raw_type = row["event_type"] or ""
    controlled = CONTROLLED_EVENT_TYPES.get(raw_type, "unknown")
    event_identity = _event_identity(listing_id, row, controlled)
    actual_at = row["event_time"] if _looks_actual(row, controlled) else None
    conn.execute(
        """
        INSERT INTO release_events(
          game_uuid,listing_id,event_identity,raw_event_type,controlled_event_type,
          announced_at,scheduled_at,actual_at,status,first_seen_at,last_seen_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(event_identity) DO UPDATE SET
          game_uuid=excluded.game_uuid,raw_event_type=excluded.raw_event_type,
          controlled_event_type=excluded.controlled_event_type,
          scheduled_at=COALESCE(NULLIF(excluded.scheduled_at,''),release_events.scheduled_at),
          actual_at=COALESCE(excluded.actual_at,release_events.actual_at),
          status=COALESCE(NULLIF(excluded.status,''),release_events.status),
          first_seen_at=MIN(release_events.first_seen_at,excluded.first_seen_at),
          last_seen_at=MAX(release_events.last_seen_at,excluded.last_seen_at)
        """,
        (
            game_uuid, listing_id, event_identity, raw_type, controlled,
            row["first_seen_at"], row["event_time"], actual_at, row["status"],
            row["first_seen_at"], row["last_seen_at"],
        ),
    )
    event_id = conn.execute(
        "SELECT id FROM release_events WHERE event_identity=?", (event_identity,)
    ).fetchone()[0]
    precision = "day" if re.match(r"^\d{4}-\d{2}-\d{2}", row["event_time"] or "") else "unknown"
    conn.execute(
        """
        INSERT OR IGNORE INTO event_schedule_versions(
          release_event_id,source_row_id,scheduled_at,event_end_at,date_precision,
          observed_at,change_reason
        ) VALUES (?,?,?,?,?,?,'observed')
        """,
        (
            event_id, row["id"], row["event_time"] or "",
            row["event_end_time"] or "", precision, row["last_seen_at"],
        ),
    )
    if controlled in {"test", "test_recruitment"}:
        status = row["status"] or ""
        round_match = re.search(r"第([一二三四五六七八九十\d]+)次|([首二三四五六七八九十])测", status)
        round_key = _normalize(round_match.group(0) if round_match else status or raw_type)
        conn.execute(
            """
            INSERT INTO test_rounds(
              release_event_id,round_key,round_index,starts_at,ends_at,reset_policy,
              billing_policy,status
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(release_event_id) DO UPDATE SET
              round_index=COALESCE(excluded.round_index,test_rounds.round_index),
              starts_at=excluded.starts_at,ends_at=excluded.ends_at,
              reset_policy=COALESCE(excluded.reset_policy,test_rounds.reset_policy),
              billing_policy=COALESCE(excluded.billing_policy,test_rounds.billing_policy),
              status=excluded.status
            """,
            (
                event_id, round_key or event_identity, _test_round_index(status),
                row["event_time"] or None,
                row["event_end_time"] or None,
                "reset" if "删档" in status and "不删档" not in status else (
                    "no_reset" if "不删档" in status else None
                ),
                "paid" if "计费" in status and "不计费" not in status else (
                    "free" if "不计费" in status else None
                ),
                status or None,
            ),
        )
    if controlled == "unknown":
        conn.execute(
            """
            INSERT INTO event_type_quarantine(
              source_row_id,raw_event_type,status,first_detected_at,last_detected_at
            ) VALUES (?,?,'pending',?,?)
            ON CONFLICT(source_row_id) DO UPDATE SET
              raw_event_type=excluded.raw_event_type,last_detected_at=excluded.last_detected_at,
              status=CASE WHEN event_type_quarantine.status='resolved'
                THEN 'resolved' ELSE 'pending' END
            """,
            (row["id"], raw_type or "(empty)", row["first_seen_at"], row["last_seen_at"]),
        )
    else:
        conn.execute(
            """
            UPDATE event_type_quarantine
            SET status='resolved',suggested_type=?,last_detected_at=?
            WHERE source_row_id=? AND status='pending'
            """,
            (controlled, row["last_seen_at"], row["id"]),
        )


def _candidate_score(conn: sqlite3.Connection, left: str, right: str) -> tuple[float, list[dict]]:
    evidence: list[dict] = [{"type": "normalized_alias", "weight": 0.10}]
    score = 0.10
    weights = {"package": 0.75, "detail_url": 0.65, "developer": 0.25}
    for evidence_type, weight in weights.items():
        values = {}
        for game_uuid in (left, right):
            values[game_uuid] = {
                row[0] for row in conn.execute(
                    """
                    SELECT normalized_value FROM identity_evidence
                    WHERE game_uuid=? AND evidence_type=? AND normalized_value<>''
                    """,
                    (game_uuid, evidence_type),
                )
            }
        shared = values[left] & values[right]
        if shared:
            score += weight
            evidence.append({
                "type": evidence_type, "weight": weight, "shared": sorted(shared)[:3],
            })
    return min(score, 1.0), evidence


def sync_phase2_model(conn: sqlite3.Connection) -> dict:
    """从当前目录无损生成第二阶段并行模型；可重复执行。"""
    now = datetime.now().astimezone().isoformat()
    conn.execute("UPDATE platform_listings SET status='stale'")
    rows = list(conn.execute(
        """
        SELECT si.*,cg.game_uuid
        FROM source_items si
        JOIN canonical_members cm ON cm.source_row_id=si.id
        JOIN canonical_games cg ON cg.id=cm.game_id
        ORDER BY si.id
        """
    ))
    listing_by_source_row: dict[int, tuple[int, str]] = {}
    for row in rows:
        listing_id, game_uuid = _sync_listing_row(conn, row, now)
        listing_by_source_row[row["id"]] = (listing_id, game_uuid)
        _sync_event(conn, row, listing_id, game_uuid)

    # 名称只召回候选，不直接合并。候选是否可自动处理由结构化佐证分数决定。
    from .catalog import identity_candidate_names
    aliases: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        "SELECT game_uuid,alias,normalized_alias FROM game_aliases WHERE status='active'"
    ):
        for candidate_alias in identity_candidate_names(row["alias"]):
            aliases[candidate_alias].add(row["game_uuid"])
    active_candidate_keys: set[tuple[str, str, str]] = set()
    for normalized_alias, game_uuids in aliases.items():
        for left, right in combinations(sorted(game_uuids), 2):
            score, evidence = _candidate_score(conn, left, right)
            status = "suggested" if score >= 0.85 else "pending"
            active_candidate_keys.add((left, right, normalized_alias))
            conn.execute(
                """
                INSERT INTO identity_candidates(
                  left_game_uuid,right_game_uuid,normalized_alias,score,evidence_json,
                  status,first_detected_at,last_detected_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(left_game_uuid,right_game_uuid,normalized_alias) DO UPDATE SET
                  score=excluded.score,evidence_json=excluded.evidence_json,
                  status=CASE WHEN identity_candidates.status IN ('merged','rejected')
                    THEN identity_candidates.status ELSE excluded.status END,
                  last_detected_at=excluded.last_detected_at
                """,
                (
                    left, right, normalized_alias, score,
                    json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                    status, now, now,
                ),
            )
            if score < 0.85:
                _upsert_review(
                    conn, queue_key=f"identity:{left}:{right}:{normalized_alias}",
                    queue_type="identity", game_uuid=left, listing_id=None,
                    priority=80, evidence={"candidate": right, "score": score, "evidence": evidence},
                    now=now,
                )

    # 已有旧目录可能曾仅凭同名合并。检测产品内部相互矛盾的包名和开发主体，
    # 不自动拆分带收藏的历史身份，而是生成高优先级拆分复核任务。
    rows_by_game: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        rows_by_game[row["game_uuid"]].append(row)
    for game_uuid, members in rows_by_game.items():
        packages = {_package_family(row["package_name"]) for row in members if _package_family(row["package_name"])}
        developers = {
            _normalize(row["developer"]) for row in members if _normalize(row["developer"])
        }
        if len(packages) < 2 or len(developers) < 2:
            continue
        clusters = sorted({
            (_package_family(row["package_name"]), _normalize(row["developer"]))
            for row in members
            if _package_family(row["package_name"]) and _normalize(row["developer"])
        })
        if len(clusters) < 2:
            continue
        _upsert_review(
            conn, queue_key=f"identity:split:{game_uuid}", queue_type="identity",
            game_uuid=game_uuid, listing_id=None, priority=95,
            evidence={
                "reason": "conflicting_package_and_developer",
                "clusters": [
                    {"package": package, "developer": developer}
                    for package, developer in clusters
                ],
                "source_row_ids": [row["id"] for row in members],
            },
            now=now,
        )

    from .gallery import extract_gallery_urls
    listing_rows: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        listing_rows[listing_by_source_row[row["id"]][0]].append(row)
    for listing_id, members in listing_rows.items():
        game_uuid = listing_by_source_row[members[0]["id"]][1]
        has_detail = any(
            len((row["full_description"] or row["gameplay_intro"] or "").strip()) >= 30
            for row in members
        )
        gallery_urls = list(dict.fromkeys(
            url for row in members for url in extract_gallery_urls(row["source"], row["raw_json"])
        ))
        for queue_type, missing, priority in (
            ("detail", not has_detail, 70), ("gallery", not gallery_urls, 60),
        ):
            queue_key = f"{queue_type}:{listing_id}"
            if missing:
                _upsert_review(
                    conn, queue_key=queue_key, queue_type=queue_type,
                    game_uuid=game_uuid, listing_id=listing_id, priority=priority,
                    evidence={
                        "source": members[0]["source"],
                        "source_item_id": members[0]["source_item_id"],
                        "latest_observed_at": max(row["last_seen_at"] for row in members),
                    },
                    now=now,
                )
            else:
                conn.execute(
                    """
                    UPDATE review_queue SET status='resolved',resolved_at=?,updated_at=?
                    WHERE queue_key=? AND status IN ('pending','retry')
                    """,
                    (now, now, queue_key),
                )
        for url in gallery_urls:
            asset = conn.execute(
                "SELECT relative_path,status,updated_at FROM screenshot_assets WHERE source_url=?",
                (url,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO media_assets(
                  game_uuid,listing_id,asset_type,source_url,relative_path,status,
                  observed_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(game_uuid,asset_type,source_url) DO UPDATE SET
                  listing_id=excluded.listing_id,relative_path=excluded.relative_path,
                  status=excluded.status,observed_at=excluded.observed_at
                """,
                (
                    game_uuid, listing_id, "screenshot", url,
                    asset["relative_path"] if asset else None,
                    asset["status"] if asset else "remote",
                    asset["updated_at"] if asset else max(row["last_seen_at"] for row in members),
                ),
            )
    return audit_phase2_model(conn)


def audit_phase2_model(conn: sqlite3.Connection) -> dict:
    """对比新旧模型的关键数量，供迁移、部署和回滚前后校验。"""
    expected_listings = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM source_items GROUP BY source,source_item_id)"
    ).fetchone()[0]
    counts = {
        "canonical_games": conn.execute("SELECT COUNT(*) FROM canonical_games").fetchone()[0],
        "games_with_uuid": conn.execute(
            "SELECT COUNT(*) FROM canonical_games WHERE game_uuid IS NOT NULL AND game_uuid<>''"
        ).fetchone()[0],
        "expected_listings": expected_listings,
        "platform_listings": conn.execute(
            "SELECT COUNT(*) FROM platform_listings WHERE status='active'"
        ).fetchone()[0],
        "source_rows": conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0],
        "listing_snapshots": conn.execute("SELECT COUNT(*) FROM listing_snapshots").fetchone()[0],
        "release_events": conn.execute("SELECT COUNT(*) FROM release_events").fetchone()[0],
        "schedule_versions": conn.execute(
            "SELECT COUNT(*) FROM event_schedule_versions"
        ).fetchone()[0],
        "unknown_event_types": conn.execute(
            "SELECT COUNT(*) FROM event_type_quarantine WHERE status='pending'"
        ).fetchone()[0],
        "pending_reviews": conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE status IN ('pending','retry')"
        ).fetchone()[0],
    }
    issues = []
    if counts["canonical_games"] != counts["games_with_uuid"]:
        issues.append("canonical_game_uuid_coverage")
    if counts["expected_listings"] != counts["platform_listings"]:
        issues.append("platform_listing_count")
    if counts["listing_snapshots"] < counts["platform_listings"]:
        issues.append("listing_snapshot_coverage")
    if counts["schedule_versions"] < counts["release_events"]:
        issues.append("schedule_version_coverage")
    return {"status": "ok" if not issues else "mismatch", "issues": issues, "counts": counts}


def claim_review_jobs(
    conn: sqlite3.Connection, queue_type: str, limit: int = 20,
) -> list[dict]:
    """领取待处理缺口并增加有界重试计数。"""
    now = datetime.now().astimezone().isoformat()
    rows = list(conn.execute(
        """
        SELECT * FROM review_queue
        WHERE queue_type=? AND status IN ('pending','retry')
          AND (next_retry_at IS NULL OR next_retry_at<=?)
        ORDER BY priority DESC,updated_at,id LIMIT ?
        """,
        (queue_type, now, max(1, min(limit, 100))),
    ))
    if rows:
        conn.executemany(
            """
            UPDATE review_queue SET status='processing',retry_count=retry_count+1,
              updated_at=? WHERE id=?
            """,
            [(now, row["id"]) for row in rows],
        )
    return [dict(row) for row in rows]


def finish_review_job(
    conn: sqlite3.Connection, job_id: int, *, status: str, result: dict,
    next_retry_at: str | None = None,
) -> None:
    if status not in {"resolved", "retry", "dismissed"}:
        raise ValueError("复核结果仅支持 resolved/retry/dismissed")
    now = datetime.now().astimezone().isoformat()
    conn.execute(
        """
        UPDATE review_queue SET status=?,result_json=?,next_retry_at=?,updated_at=?,
          resolved_at=CASE WHEN ? IN ('resolved','dismissed') THEN ? ELSE NULL END
        WHERE id=?
        """,
        (
            status, json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            next_retry_at, now, status, now, job_id,
        ),
    )
