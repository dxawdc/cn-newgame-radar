"""本机模拟器采集结果的增量打包与服务器安全导入。"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import tarfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath

from .catalog import audit_catalog_completeness, rebuild_catalog
from .db import connect, upsert_items
from .gallery import cache_remote_screenshots
from .icon_cache import cache_remote_icons


SCHEMA_VERSION = 1
ITEM_SOURCES = {
    "taptap",
    "huawei_gamecenter",
    "honor_gamecenter",
    "oppo_gamecenter",
}
RUN_SOURCES = {
    "taptap",
    "huawei-cache",
    "honor-ui",
    "oppo-ui",
}
MAX_MEMBERS = 5000
MAX_TOTAL_BYTES = 512 * 1024 * 1024


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


def export_bundle(
    db_path: Path,
    raw_dir: Path,
    icon_dir: Path,
    output_path: Path,
    since: str,
) -> dict:
    """导出本轮模拟器采集到的记录、UI 图标和原始证据。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ITEM_SOURCES)
    items = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT * FROM source_items
            WHERE source IN ({placeholders}) AND last_seen_at >= ?
            ORDER BY source, id
            """,
            (*sorted(ITEM_SOURCES), since),
        )
    ]
    run_placeholders = ",".join("?" for _ in RUN_SOURCES)
    runs = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT source, started_at, finished_at, status, item_count, error
            FROM collection_runs
            WHERE source IN ({run_placeholders}) AND started_at >= ?
            ORDER BY id
            """,
            (*sorted(RUN_SOURCES), since),
        )
    ]
    conn.close()

    successful = {row["source"] for row in runs if row["status"] == "success"}
    missing = RUN_SOURCES - successful
    if missing:
        raise RuntimeError(f"以下模拟器采集器没有成功记录：{', '.join(sorted(missing))}")
    if not items:
        raise RuntimeError("本轮模拟器采集没有产生可同步记录")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().astimezone().isoformat(),
        "since": since,
        "item_sources": sorted(ITEM_SOURCES),
        "run_sources": sorted(RUN_SOURCES),
        "items": items,
        "runs": runs,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = raw_dir.resolve()
    icon_dir = icon_dir.resolve()
    included_icons: set[str] = set()
    with tarfile.open(output_path, "w:gz") as archive:
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.now().timestamp())
        archive.addfile(info, io.BytesIO(manifest_bytes))

        for item in items:
            icon_url = item.get("icon_url") or ""
            prefix = "local-icon://"
            if not icon_url.startswith(prefix):
                continue
            relative = PurePosixPath(icon_url[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            relative_text = relative.as_posix()
            if relative_text in included_icons:
                continue
            source_path = icon_dir.joinpath(*relative.parts)
            if source_path.is_file() and _is_within(source_path, icon_dir):
                archive.add(source_path, arcname=f"icons/{relative_text}", recursive=False)
                included_icons.add(relative_text)

        if raw_dir.is_dir():
            for source_path in raw_dir.rglob("*"):
                if source_path.is_file() and _is_within(source_path, raw_dir):
                    relative = source_path.relative_to(raw_dir).as_posix()
                    archive.add(source_path, arcname=f"raw/{relative}", recursive=False)

    return {
        "bundle": str(output_path),
        "items": len(items),
        "runs": len(runs),
        "icons": len(included_icons),
    }


def import_bundle(
    bundle_path: Path,
    db_path: Path,
    raw_dir: Path,
    icon_dir: Path,
    screenshot_dir: Path | None = None,
    cache_icons: bool = True,
) -> dict:
    """将可信增量包按业务唯一键合并进服务器数据库。"""
    with tarfile.open(bundle_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError("增量包文件数量超过安全上限")
        total_bytes = sum(member.size for member in members if member.isfile())
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("增量包体积超过安全上限")
        by_name = {}
        for member in members:
            _safe_member_path(member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"增量包包含不支持的成员类型：{member.name}")
            by_name[member.name] = member
        manifest_member = by_name.get("manifest.json")
        if not manifest_member or not manifest_member.isfile():
            raise ValueError("增量包缺少 manifest.json")
        manifest_file = archive.extractfile(manifest_member)
        if manifest_file is None:
            raise ValueError("无法读取增量包清单")
        manifest = json.loads(manifest_file.read().decode("utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("不支持的增量包版本")
        if set(manifest.get("item_sources", [])) - ITEM_SOURCES:
            raise ValueError("增量包包含未授权的数据渠道")

        copied_raw = 0
        copied_icons = 0
        for member in members:
            if not member.isfile() or member.name == "manifest.json":
                continue
            relative = _safe_member_path(member.name)
            if relative.parts[0] == "raw":
                root = raw_dir.resolve()
                target = root.joinpath(*relative.parts[1:])
                copied_raw += 1
            elif relative.parts[0] == "icons":
                root = icon_dir.resolve()
                target = root.joinpath(*relative.parts[1:])
                copied_icons += 1
            else:
                raise ValueError(f"增量包包含未知目录：{member.name}")
            if not _is_within(target, root):
                raise ValueError(f"增量包目标路径越界：{member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"无法读取增量文件：{member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)

    items = manifest.get("items", [])
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in items:
        if row.get("source") not in ITEM_SOURCES:
            raise ValueError(f"记录包含未授权渠道：{row.get('source')}")
        observed_at = row.get("last_seen_at") or manifest["created_at"]
        item = {
            key: value
            for key, value in row.items()
            if key not in {"id", "tags_json", "raw_json", "first_seen_at", "last_seen_at"}
        }
        item["tags"] = json.loads(row.get("tags_json") or "[]")
        item["raw"] = json.loads(row.get("raw_json") or "{}")
        grouped[observed_at].append(item)

    conn = connect(db_path)
    imported = 0
    for observed_at, group in grouped.items():
        imported += upsert_items(conn, group, observed_at)
    imported_runs = 0
    for run in manifest.get("runs", []):
        if run.get("source") not in RUN_SOURCES:
            continue
        exists = conn.execute(
            "SELECT 1 FROM collection_runs WHERE source=? AND started_at=?",
            (run["source"], run["started_at"]),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO collection_runs(source, started_at, finished_at, status, item_count, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run["source"], run["started_at"], run.get("finished_at"),
                run["status"], run.get("item_count") or 0, run.get("error"),
            ),
        )
        imported_runs += 1
    conn.commit()
    games = rebuild_catalog(conn)
    completeness = audit_catalog_completeness(conn)
    icons = cache_remote_icons(conn, icon_dir) if cache_icons else None
    screenshots = cache_remote_screenshots(conn, screenshot_dir) if screenshot_dir else None
    conn.close()
    return {
        "items": imported,
        "runs": imported_runs,
        "raw_files": copied_raw,
        "icon_files": copied_icons,
        "games": games,
        "completeness": completeness,
        "icons": icons,
        "screenshots": screenshots,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟器采集增量包工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--db", type=Path, required=True)
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
        result = export_bundle(args.db, args.raw_dir, args.icon_dir, args.output, args.since)
    else:
        result = import_bundle(
            args.bundle, args.db, args.raw_dir, args.icon_dir, args.screenshot_dir,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
