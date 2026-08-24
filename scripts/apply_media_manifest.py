"""将已核验的渠道媒体字段增量合并到现有数据库。

该脚本只允许写入图集/宣传图字段，不替换账号、关注、API Key 等业务数据。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


ALLOWED_PATHS = {
    "taptap": {("detail_screenshots",), ("detail_banner",)},
    "xiaomi_gamecenter": {("official_public_detail", "screenshots")},
    "haoyou_kuaibao": {("detail_screenshot_urls",)},
    "4399_gamebox": {("info_data_detail", "screenshot_urls")},
    "honor_gamecenter": {("honor_cache_detail", "screenshot_urls")},
    "oppo_gamecenter": {("oppo_offline_detail", "screenshot_urls")},
}


def _leaf_paths(value: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if not isinstance(value, dict):
        return {prefix}
    paths: set[tuple[str, ...]] = set()
    for key, child in value.items():
        paths.update(_leaf_paths(child, (*prefix, str(key))))
    return paths


def _deep_merge(target: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _paths_are_allowed(source: str, patch: dict) -> bool:
    """允许白名单字段本身是对象，但不允许越出该字段的任意兄弟节点。"""
    return all(
        any(path[: len(allowed)] == allowed for allowed in ALLOWED_PATHS[source])
        for path in _leaf_paths(patch)
    )


def apply_manifest(db_path: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("不支持的媒体清单版本")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    matched_entries = 0
    updated_rows = 0
    try:
        with conn:
            for entry in manifest.get("entries", []):
                source = str(entry.get("source") or "")
                source_item_id = str(entry.get("source_item_id") or "")
                patch = entry.get("raw_patch")
                if source not in ALLOWED_PATHS or not source_item_id or not isinstance(patch, dict):
                    raise ValueError("媒体清单包含未授权的记录")
                if not _paths_are_allowed(source, patch):
                    raise ValueError(f"媒体清单包含未授权的字段：{source}/{source_item_id}")

                rows = list(
                    conn.execute(
                        "SELECT id, raw_json FROM source_items WHERE source=? AND source_item_id=?",
                        (source, source_item_id),
                    )
                )
                if not rows:
                    continue
                matched_entries += 1
                for row in rows:
                    try:
                        raw = json.loads(row["raw_json"] or "{}")
                    except json.JSONDecodeError:
                        raw = {}
                    merged = _deep_merge(raw if isinstance(raw, dict) else {}, patch)
                    conn.execute(
                        "UPDATE source_items SET raw_json=? WHERE id=?",
                        (json.dumps(merged, ensure_ascii=False, separators=(",", ":")), row["id"]),
                    )
                    updated_rows += 1
    finally:
        conn.close()
    return {
        "manifest_entries": len(manifest.get("entries", [])),
        "matched_entries": matched_entries,
        "updated_rows": updated_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="合并已核验的渠道图集字段")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(apply_manifest(args.db, args.manifest), ensure_ascii=False))


if __name__ == "__main__":
    main()
