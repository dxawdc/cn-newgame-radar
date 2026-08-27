"""从渠道原始详情中提取商店截图，并缓存为本站可稳定访问的图片。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 NewGameMonitor/1.0",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _relative_path(url: str) -> Path:
    return Path("remote") / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]}.webp"


def _url(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.startswith(("https://", "http://", "local-screenshot://")) else None
    if isinstance(value, dict):
        for key in ("url", "medium_url", "original_url", "imageUrl", "imgUrl"):
            result = _url(value.get(key))
            if result:
                return result
    return None


def _urls(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [result for value in values if (result := _url(value))]


def extract_gallery_urls(source: str, raw_value: str | dict | None) -> list[str]:
    """只读取明确的截图字段，避免把 Icon、头像或广告图混入五图。"""
    if isinstance(raw_value, str):
        try:
            raw = json.loads(raw_value or "{}")
        except json.JSONDecodeError:
            return []
    else:
        raw = raw_value or {}
    if not isinstance(raw, dict):
        return []

    candidates: list[str] = []
    if source == "taptap":
        candidates.extend(_urls(raw.get("detail_screenshots")))
        candidates.extend(_urls((raw.get("app_card_info") or {}).get("screenshots")))
        candidates.extend(_urls(raw.get("screenshots")))
        if not candidates:
            for value in (
                raw.get("detail_banner"),
                (raw.get("app_card_info") or {}).get("banner"),
                (raw.get("app_card_info") or {}).get("top_banner"),
                (raw.get("app_card_info") or {}).get("ad_banner"),
            ):
                if result := _url(value):
                    candidates.append(result)
    elif source == "apple_appstore_cn":
        candidates.extend(_urls(raw.get("screenshot_urls")))
    elif source == "xiaomi_gamecenter":
        public = raw.get("official_public_detail") or {}
        candidates.extend(_urls(public.get("screenshots")))
    elif source == "vivo_gamecenter":
        candidates.extend(_urls((raw.get("burstInfo") or {}).get("screenShots")))
        candidates.extend(_urls((raw.get("vivo_appointment_detail") or {}).get("screenShots")))
        if not candidates:
            for value in (
                (raw.get("additionalImages") or {}).get("waterfall"),
                (raw.get("video") or {}).get("videoImageUrl"),
                (raw.get("burstInfo") or {}).get("originaBkgImage"),
                (raw.get("burstInfo") or {}).get("componentCardImage"),
                (raw.get("burstInfo") or {}).get("videoImage"),
            ):
                if result := _url(value):
                    candidates.append(result)
    elif source == "huawei_gamecenter":
        public = raw.get("official_public_detail") or {}
        for key in ("gScreenShots", "screenShots", "screenshots"):
            candidates.extend(_urls(raw.get(key)))
            candidates.extend(_urls(public.get(key)))
        # 部分华为详情只返回横版商店大图，作为截图不足时的降级内容。
        candidates.extend(_urls(raw.get("bigImages")))
        candidates.extend(_urls(public.get("bigImages")))
    elif source == "233_leyuan":
        detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else raw
        candidates.extend(_urls(detail.get("imageUrls")))
        candidates.extend(_urls(detail.get("images")))
        if not candidates:
            candidates.extend(_urls(detail.get("bigPictures")))
            for video in detail.get("videos") or []:
                if result := _url(video.get("videoImageUrl") if isinstance(video, dict) else None):
                    candidates.append(result)
    elif source == "uc_9game":
        detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else raw
        candidates.extend(_urls(detail.get("screenshot_urls")))
    elif source == "haoyou_kuaibao":
        candidates.extend(_urls(raw.get("detail_screenshot_urls")))
    elif source == "4399_gamebox":
        detail = raw.get("info_data_detail") or {}
        candidates.extend(_urls(detail.get("screenshot_urls")))
    elif source == "honor_gamecenter":
        detail = raw.get("honor_cache_detail") or {}
        candidates.extend(_urls(detail.get("screenshot_urls")))
    elif source == "oppo_gamecenter":
        detail = raw.get("oppo_offline_detail") or {}
        candidates.extend(_urls(detail.get("screenshot_urls")))
        ui_detail = raw.get("ui_detail") or {}
        candidates.extend(_urls(ui_detail.get("screenshot_urls")))

    return list(dict.fromkeys(candidates))[:10]


def gallery_urls_from_rows(rows) -> list[tuple[str, str]]:
    """按渠道记录顺序返回 (来源, 图片 URL)，由上层决定最终最多展示几张。"""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        raw_json = row["raw_json"] if "raw_json" in row.keys() else {}
        for url in extract_gallery_urls(row["source"], raw_json):
            if url not in seen:
                seen.add(url)
                result.append((row["source"], url))
    return result


def _download(url: str, screenshot_dir: Path) -> dict:
    now = datetime.now().astimezone().isoformat()
    try:
        response = requests.get(url, headers=HEADERS, timeout=25)
        response.raise_for_status()
        if len(response.content) > 15 * 1024 * 1024:
            raise ValueError("图片超过 15MB 限制")
        image = Image.open(BytesIO(response.content))
        image.seek(0)
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        relative = _relative_path(url)
        target = screenshot_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "WEBP", quality=84, method=4)
        return {
            "source_url": url, "relative_path": relative.as_posix(), "status": "success",
            "http_status": response.status_code, "content_type": response.headers.get("content-type"),
            "byte_size": target.stat().st_size, "updated_at": now, "error": None,
        }
    except Exception as exc:
        return {
            "source_url": url, "relative_path": None, "status": "failed",
            "http_status": getattr(getattr(exc, "response", None), "status_code", None),
            "content_type": None, "byte_size": None, "updated_at": now, "error": str(exc)[:500],
        }


def cache_remote_screenshots(
    conn: sqlite3.Connection, screenshot_dir: Path, workers: int = 6,
) -> dict:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    urls: set[str] = set()
    for row in conn.execute("SELECT source, raw_json FROM source_items"):
        urls.update(extract_gallery_urls(row["source"], row["raw_json"])[:5])
    successful = {
        row[0]: row[1] for row in conn.execute(
            "SELECT source_url, relative_path FROM screenshot_assets WHERE status='success'"
        )
        if row[1] and (screenshot_dir / row[1]).exists()
    }
    # 发布包可能已携带缓存文件但尚未把索引迁移到服务器数据库；直接认领，避免重复下载。
    discovered = []
    now = datetime.now().astimezone().isoformat()
    for url in sorted(urls - set(successful)):
        relative = _relative_path(url)
        target = screenshot_dir / relative
        if target.is_file():
            discovered.append({
                "source_url": url, "relative_path": relative.as_posix(), "status": "success",
                "http_status": 200, "content_type": "image/webp",
                "byte_size": target.stat().st_size, "updated_at": now, "error": None,
            })
            successful[url] = relative.as_posix()
    if discovered:
        with conn:
            conn.executemany(
                """
                INSERT INTO screenshot_assets (
                    source_url, relative_path, status, http_status, content_type,
                    byte_size, updated_at, error
                ) VALUES (
                    :source_url, :relative_path, :status, :http_status, :content_type,
                    :byte_size, :updated_at, :error
                )
                ON CONFLICT(source_url) DO UPDATE SET
                    relative_path=excluded.relative_path, status=excluded.status,
                    http_status=excluded.http_status, content_type=excluded.content_type,
                    byte_size=excluded.byte_size, updated_at=excluded.updated_at, error=excluded.error
                """,
                discovered,
            )
    pending = sorted(
        url for url in urls - set(successful)
        if url.startswith(("https://", "http://"))
    )
    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download, url, screenshot_dir): url for url in pending}
            for future in as_completed(futures):
                results.append(future.result())
        with conn:
            conn.executemany(
                """
                INSERT INTO screenshot_assets (
                    source_url, relative_path, status, http_status, content_type,
                    byte_size, updated_at, error
                ) VALUES (
                    :source_url, :relative_path, :status, :http_status, :content_type,
                    :byte_size, :updated_at, :error
                )
                ON CONFLICT(source_url) DO UPDATE SET
                    relative_path=excluded.relative_path, status=excluded.status,
                    http_status=excluded.http_status, content_type=excluded.content_type,
                    byte_size=excluded.byte_size, updated_at=excluded.updated_at, error=excluded.error
                """,
                results,
            )
    return {
        "known": len(urls),
        "cached": len(urls & set(successful)) + sum(
            item["status"] == "success" for item in results
        ),
        "downloaded": sum(item["status"] == "success" for item in results),
        "discovered": len(discovered),
        "failed": sum(item["status"] == "failed" for item in results),
    }
