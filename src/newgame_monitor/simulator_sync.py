"""本机模拟器采集结果的差异打包与服务器原子导入。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath

from .catalog import audit_catalog_completeness, rebuild_catalog
from .db import begin_immediate_with_retry, connect, upsert_items
from .gallery import cache_remote_screenshots
from .icon_cache import cache_remote_icons


SCHEMA_VERSION = 2
RUN_TO_ITEM = {
    "taptap": "taptap",
    "huawei-cache": "huawei_gamecenter",
    "honor-ui": "honor_gamecenter",
    "oppo-ui": "oppo_gamecenter",
}
ITEM_SOURCES = set(RUN_TO_ITEM.values())
RUN_SOURCES = set(RUN_TO_ITEM)
PUBLISHABLE_RUN_STATUSES = {"success", "degraded"}
MAX_MEMBERS = 5000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
BUSINESS_KEY = ("source", "source_item_id", "event_type", "event_time")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"增量包包含不安全路径：{name}")
    return path


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_screenshot_paths(value) -> set[str]:
    """递归读取需要随数据包同步的本地图片。"""
    prefixes = ("local-screenshot://", "local-icon://")
    found: set[str] = set()
    if isinstance(value, str):
        for prefix in prefixes:
            if value.startswith(prefix):
                found.add(value[len(prefix):])
                break
    elif isinstance(value, dict):
        for child in value.values():
            found.update(_local_screenshot_paths(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_local_screenshot_paths(child))
    return found


def _read_rows(db_path: Path, sources: set[str]) -> dict[tuple, dict]:
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in sources)
    rows = {
        tuple(row[field] for field in BUSINESS_KEY): dict(row)
        for row in conn.execute(
            f"SELECT * FROM source_items WHERE source IN ({placeholders})",
            tuple(sorted(sources)),
        )
    }
    conn.close()
    return rows


def _row_signature(row: dict) -> str:
    # id 是单库自增值，canonical_key 由目标端重建，都不参与差异判定。
    comparable = {key: value for key, value in row.items() if key not in {"id", "canonical_key"}}
    return json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _latest_runs(db_path: Path, since: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in RUN_SOURCES)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT run_group_id,source,started_at,finished_at,status,item_count,error,metrics_json
            FROM collection_runs
            WHERE source IN ({placeholders}) AND started_at >= ?
            ORDER BY id
            """,
            (*sorted(RUN_SOURCES), since),
        )
    ]
    conn.close()
    return rows


def _pipeline_payload(db_path: Path, since: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            """
            SELECT * FROM pipeline_runs
            WHERE started_at>=? ORDER BY started_at DESC LIMIT 1
            """,
            (since,),
        ).fetchone()
        if not run:
            return None
        stages = [
            dict(row) for row in conn.execute(
                "SELECT * FROM pipeline_stages WHERE run_id=? ORDER BY id", (run["run_id"],)
            )
        ]
        return {"run": dict(run), "stages": stages}
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _max_change_id(db_path: Path | None) -> int:
    if not db_path or not db_path.is_file():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id),0) FROM source_item_changes").fetchone()
        return int(row[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _add_file(files: dict[str, Path], archive_name: str, source_path: Path) -> None:
    _safe_member_path(archive_name)
    if archive_name in files:
        if files[archive_name].resolve() != source_path.resolve():
            raise ValueError(f"增量包文件名冲突：{archive_name}")
        return
    files[archive_name] = source_path


def export_bundle(
    db_path: Path,
    raw_dir: Path,
    icon_dir: Path,
    output_path: Path,
    since: str,
    base_db_path: Path | None = None,
) -> dict:
    """导出成功或降级成功渠道的差异；失败渠道保持服务端原数据。"""
    runs = _latest_runs(db_path, since)
    latest_by_source: dict[str, dict] = {}
    for run in runs:
        latest_by_source[run["source"]] = run
    publishable_runs = {
        source for source, run in latest_by_source.items()
        if run["status"] in PUBLISHABLE_RUN_STATUSES
    }
    if not publishable_runs:
        failures = [
            f'{source}: {(latest_by_source.get(source) or {}).get("error") or "无运行记录"}'
            for source in sorted(RUN_SOURCES)
        ]
        raise RuntimeError("本轮没有可发布渠道；" + "；".join(failures))

    publishable_item_sources = {RUN_TO_ITEM[source] for source in publishable_runs}
    current = _read_rows(db_path, publishable_item_sources)
    if base_db_path:
        base = _read_rows(base_db_path, publishable_item_sources)
        items = [
            row for key, row in current.items()
            if key not in base or _row_signature(row) != _row_signature(base[key])
        ]
        tombstones = [
            dict(zip(BUSINESS_KEY, key))
            for key in sorted(base.keys() - current.keys())
        ]
    else:
        items = [row for row in current.values() if (row.get("updated_at") or row["last_seen_at"]) >= since]
        tombstones = []
    items.sort(key=lambda row: (row["source"], row.get("id", 0)))

    raw_root = raw_dir.resolve()
    icon_root = icon_dir.resolve()
    files: dict[str, Path] = {}
    for item in items:
        local_paths = _local_screenshot_paths(item.get("icon_url") or "")
        try:
            local_paths.update(_local_screenshot_paths(json.loads(item.get("raw_json") or "{}")))
        except json.JSONDecodeError:
            pass
        for local_path in local_paths:
            relative = PurePosixPath(local_path)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source_path = icon_root.joinpath(*relative.parts)
            if source_path.is_file() and _is_within(source_path, icon_root):
                _add_file(files, f"icons/{relative.as_posix()}", source_path)

    if raw_root.is_dir():
        since_timestamp = datetime.fromisoformat(since).timestamp()
        for source_path in raw_root.rglob("*"):
            if (
                source_path.is_file()
                and source_path.stat().st_mtime >= since_timestamp
                and _is_within(source_path, raw_root)
            ):
                relative = source_path.relative_to(raw_root).as_posix()
                _add_file(files, f"raw/{relative}", source_path)

    file_records = [
        {"path": name, "size": path.stat().st_size, "sha256": _sha256_file(path)}
        for name, path in sorted(files.items())
    ]
    bundle_id = str(uuid.uuid4())
    failed_runs = sorted(RUN_SOURCES - publishable_runs)
    pipeline = _pipeline_payload(db_path, since)
    if pipeline:
        pipeline["run"]["bundle_id"] = bundle_id
        pipeline["stages"].append({
            "run_id": pipeline["run"]["run_id"],
            "stage": "validated",
            "source": "",
            "started_at": datetime.now().astimezone().isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
            "status": "success",
            "detail_json": json.dumps({"bundle_id": bundle_id}, separators=(",", ":")),
        })
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "since": since,
        "cursor": {
            "from_change_id": _max_change_id(base_db_path),
            "to_change_id": _max_change_id(db_path),
        },
        "item_sources": sorted(publishable_item_sources),
        "run_sources": sorted(publishable_runs),
        "failed_run_sources": failed_runs,
        "publish_status": "partial" if failed_runs else "complete",
        "items": items,
        "tombstones": tombstones,
        "runs": runs,
        "pipeline": pipeline,
        "files": file_records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with tarfile.open(output_path, "w:gz") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.now().timestamp())
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for archive_name, source_path in sorted(files.items()):
            archive.add(source_path, arcname=archive_name, recursive=False)

    if pipeline:
        conn = connect(db_path)
        conn.execute(
            "UPDATE pipeline_runs SET bundle_id=? WHERE run_id=?",
            (bundle_id, pipeline["run"]["run_id"]),
        )
        conn.commit()
        conn.close()

    return {
        "bundle": str(output_path),
        "bundle_id": bundle_id,
        "publish_status": manifest["publish_status"],
        "published_sources": sorted(publishable_runs),
        "failed_sources": failed_runs,
        "items": len(items),
        "tombstones": len(tombstones),
        "runs": len(runs),
        "files": len(files),
        "icons": sum(name.startswith("icons/") for name in files),
        "raw_files": sum(name.startswith("raw/") for name in files),
    }


def _validate_and_stage_bundle(bundle_path: Path, stage_root: Path) -> tuple[dict, bytes]:
    with tarfile.open(bundle_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError("增量包文件数量超过安全上限")
        if sum(member.size for member in members if member.isfile()) > MAX_TOTAL_BYTES:
            raise ValueError("增量包体积超过安全上限")
        by_name = {}
        for member in members:
            _safe_member_path(member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"增量包包含不支持的成员类型：{member.name}")
            if member.name in by_name:
                raise ValueError(f"增量包包含重复路径：{member.name}")
            by_name[member.name] = member
        manifest_member = by_name.get("manifest.json")
        if not manifest_member or not manifest_member.isfile():
            raise ValueError("增量包缺少 manifest.json")
        manifest_file = archive.extractfile(manifest_member)
        if manifest_file is None:
            raise ValueError("无法读取增量包清单")
        manifest_bytes = manifest_file.read()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("不支持的增量包版本")
        try:
            uuid.UUID(manifest["bundle_id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("增量包缺少合法 bundle_id") from exc
        if set(manifest.get("item_sources", [])) - ITEM_SOURCES:
            raise ValueError("增量包包含未授权的数据渠道")
        if set(manifest.get("run_sources", [])) - RUN_SOURCES:
            raise ValueError("增量包包含未授权的运行渠道")

        expected_files = {row["path"]: row for row in manifest.get("files", [])}
        actual_files = {
            name for name, member in by_name.items()
            if member.isfile() and name != "manifest.json"
        }
        if actual_files != set(expected_files):
            raise ValueError("增量包文件列表与清单不一致")
        for name, record in expected_files.items():
            relative = _safe_member_path(name)
            if relative.parts[0] not in {"raw", "icons"}:
                raise ValueError(f"增量包包含未知目录：{name}")
            source = archive.extractfile(by_name[name])
            if source is None:
                raise ValueError(f"无法读取增量文件：{name}")
            payload = source.read()
            if len(payload) != record.get("size") or _sha256_bytes(payload) != record.get("sha256"):
                raise ValueError(f"增量文件完整性校验失败：{name}")
            target = stage_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    return manifest, manifest_bytes


def _media_plan(manifest: dict, stage_root: Path, raw_dir: Path, icon_dir: Path) -> list[tuple[Path, Path, Path]]:
    plan = []
    for record in manifest.get("files", []):
        relative = _safe_member_path(record["path"])
        root = raw_dir.resolve() if relative.parts[0] == "raw" else icon_dir.resolve()
        target = root.joinpath(*relative.parts[1:])
        if not _is_within(target, root):
            raise ValueError(f'增量包目标路径越界：{record["path"]}')
        if target.exists() and (not target.is_file() or _sha256_file(target) != record["sha256"]):
            raise ValueError(f'正式媒体目标已存在且内容不同：{record["path"]}')
        plan.append((stage_root.joinpath(*relative.parts), target, root))
    return plan


def _remove_created(paths: list[tuple[Path, Path]]) -> None:
    for path, root in reversed(paths):
        try:
            path.unlink(missing_ok=True)
            parent = path.parent
            while parent != root and _is_within(parent, root):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass


def import_bundle(
    bundle_path: Path,
    db_path: Path,
    raw_dir: Path,
    icon_dir: Path,
    screenshot_dir: Path | None = None,
    cache_icons: bool = True,
) -> dict:
    """先全量校验并 staging，再将数据、运行记录、目录和回执单事务发布。"""
    with tempfile.TemporaryDirectory(prefix="newgame-bundle-") as temporary:
        stage_root = Path(temporary)
        manifest, manifest_bytes = _validate_and_stage_bundle(bundle_path, stage_root)
        manifest_sha256 = _sha256_bytes(manifest_bytes)
        allowed_item_sources = set(manifest.get("item_sources", []))
        for row in manifest.get("items", []):
            if row.get("source") not in allowed_item_sources:
                raise ValueError(f"记录包含未授权渠道：{row.get('source')}")
        for row in manifest.get("tombstones", []):
            if row.get("source") not in allowed_item_sources:
                raise ValueError(f"删除标记包含未授权渠道：{row.get('source')}")
            if any(field not in row for field in BUSINESS_KEY):
                raise ValueError("删除标记缺少业务主键")
        media_plan = _media_plan(manifest, stage_root, raw_dir, icon_dir)

        conn = connect(db_path)
        existing = conn.execute(
            "SELECT result_json FROM applied_bundles WHERE bundle_id=?",
            (manifest["bundle_id"],),
        ).fetchone()
        if existing:
            conn.close()
            result = json.loads(existing["result_json"] or "{}")
            result.update({"bundle_id": manifest["bundle_id"], "duplicate": True})
            return result

        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in manifest.get("items", []):
            observed_at = row.get("updated_at") or row.get("last_seen_at") or manifest["created_at"]
            item = {
                key: value
                for key, value in row.items()
                if key not in {
                    "id", "tags_json", "raw_json", "first_seen_at", "last_seen_at",
                    "updated_at", "canonical_key",
                }
            }
            item["tags"] = json.loads(row.get("tags_json") or "[]")
            item["raw"] = json.loads(row.get("raw_json") or "{}")
            grouped[observed_at].append(item)

        created_media: list[tuple[Path, Path]] = []
        try:
            begin_immediate_with_retry(conn)
            imported = 0
            for observed_at, group in grouped.items():
                imported += upsert_items(conn, group, observed_at, commit=False)
            deleted = 0
            for tombstone in manifest.get("tombstones", []):
                cursor = conn.execute(
                    """
                    DELETE FROM source_items
                    WHERE source=? AND source_item_id=? AND event_type=? AND event_time=?
                    """,
                    tuple(tombstone[field] for field in BUSINESS_KEY),
                )
                deleted += cursor.rowcount

            imported_runs = 0
            for run in manifest.get("runs", []):
                if run.get("source") not in RUN_SOURCES:
                    continue
                cursor = conn.execute(
                    """
                    INSERT INTO collection_runs(
                      run_group_id,source,started_at,finished_at,status,item_count,error,metrics_json
                    )
                    SELECT ?,?,?,?,?,?,?,?
                    WHERE NOT EXISTS (
                      SELECT 1 FROM collection_runs WHERE source=? AND started_at=?
                    )
                    """,
                    (
                        run.get("run_group_id"), run["source"], run["started_at"],
                        run.get("finished_at"), run["status"], run.get("item_count") or 0,
                        run.get("error"), run.get("metrics_json") or "{}",
                        run["source"], run["started_at"],
                    ),
                )
                imported_runs += cursor.rowcount

            pipeline = manifest.get("pipeline") or {}
            pipeline_run = pipeline.get("run")
            if pipeline_run:
                conn.execute(
                    """
                    INSERT INTO pipeline_runs(
                      run_id,started_at,finished_at,status,bundle_id,summary_json
                    ) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                      finished_at=excluded.finished_at,status=excluded.status,
                      bundle_id=excluded.bundle_id,summary_json=excluded.summary_json
                    """,
                    (
                        pipeline_run["run_id"], pipeline_run["started_at"],
                        pipeline_run.get("finished_at"), pipeline_run.get("status") or "partial",
                        manifest["bundle_id"], pipeline_run.get("summary_json") or "{}",
                    ),
                )
                for stage in pipeline.get("stages", []):
                    conn.execute(
                        """
                        INSERT INTO pipeline_stages(
                          run_id,stage,source,started_at,finished_at,status,detail_json
                        ) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,stage,source) DO UPDATE SET
                          started_at=excluded.started_at,finished_at=excluded.finished_at,
                          status=excluded.status,detail_json=excluded.detail_json
                        """,
                        (
                            pipeline_run["run_id"], stage["stage"], stage.get("source") or "",
                            stage["started_at"], stage.get("finished_at"), stage["status"],
                            stage.get("detail_json") or "{}",
                        ),
                    )

            games = rebuild_catalog(conn, manage_transaction=False)
            completeness = audit_catalog_completeness(conn)
            for staged, target, root in media_plan:
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with staged.open("rb") as source, target.open("xb") as destination:
                    created_media.append((target, root))
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())

            result = {
                "bundle_id": manifest["bundle_id"],
                "duplicate": False,
                "publish_status": "partial" if manifest.get("failed_run_sources") else "published",
                "published_sources": manifest.get("run_sources", []),
                "failed_sources": manifest.get("failed_run_sources", []),
                "items": imported,
                "deleted": deleted,
                "runs": imported_runs,
                "raw_files": sum(record["path"].startswith("raw/") for record in manifest.get("files", [])),
                "icon_files": sum(record["path"].startswith("icons/") for record in manifest.get("files", [])),
                "games": games,
                "completeness": completeness,
                "cursor": manifest.get("cursor", {}),
            }
            applied_at = datetime.now().astimezone().isoformat()
            if pipeline_run:
                published_status = "partial" if manifest.get("failed_run_sources") else "published"
                for stage_name in ("imported", "published"):
                    conn.execute(
                        """
                        INSERT INTO pipeline_stages(
                          run_id,stage,source,started_at,finished_at,status,detail_json
                        ) VALUES (?,?, '',?,?,?,?)
                        ON CONFLICT(run_id,stage,source) DO UPDATE SET
                          finished_at=excluded.finished_at,status=excluded.status,
                          detail_json=excluded.detail_json
                        """,
                        (
                            pipeline_run["run_id"], stage_name, applied_at, applied_at,
                            published_status,
                            json.dumps({"bundle_id": manifest["bundle_id"]}, separators=(",", ":")),
                        ),
                    )
                conn.execute(
                    """
                    UPDATE pipeline_runs SET finished_at=?,status=?,bundle_id=?,summary_json=?
                    WHERE run_id=?
                    """,
                    (
                        applied_at, published_status, manifest["bundle_id"],
                        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        pipeline_run["run_id"],
                    ),
                )
            conn.execute(
                """
                INSERT INTO applied_bundles(
                  bundle_id,schema_version,created_at,applied_at,manifest_sha256,result_json
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    manifest["bundle_id"], SCHEMA_VERSION, manifest["created_at"], applied_at,
                    manifest_sha256, json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            _remove_created(created_media)
            conn.close()
            raise

        postprocess_errors = []
        icons = None
        screenshots = None
        try:
            icons = cache_remote_icons(conn, icon_dir) if cache_icons else None
        except Exception as exc:  # 导入已发布，缓存失败降级记录。
            postprocess_errors.append(f"icon_cache: {exc}")
        try:
            screenshots = cache_remote_screenshots(conn, screenshot_dir) if screenshot_dir else None
        except Exception as exc:
            postprocess_errors.append(f"screenshot_cache: {exc}")
        result.update({"icons": icons, "screenshots": screenshots, "postprocess_errors": postprocess_errors})
        conn.execute(
            "UPDATE applied_bundles SET result_json=? WHERE bundle_id=?",
            (json.dumps(result, ensure_ascii=False, separators=(",", ":")), manifest["bundle_id"]),
        )
        conn.commit()
        conn.close()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟器采集差异包工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--db", type=Path, required=True)
    export_parser.add_argument("--base-db", type=Path)
    export_parser.add_argument("--raw-dir", type=Path, required=True)
    export_parser.add_argument("--icon-dir", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--since", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--bundle", type=Path, required=True)
    import_parser.add_argument("--db", type=Path, required=True)
    import_parser.add_argument("--raw-dir", type=Path, required=True)
    import_parser.add_argument("--icon-dir", type=Path, required=True)
    import_parser.add_argument("--screenshot-dir", type=Path, default=Path("data/screenshots"))
    args = parser.parse_args()

    if args.command == "export":
        result = export_bundle(
            args.db, args.raw_dir, args.icon_dir, args.output, args.since, args.base_db,
        )
    else:
        result = import_bundle(
            args.bundle, args.db, args.raw_dir, args.icon_dir, args.screenshot_dir,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
