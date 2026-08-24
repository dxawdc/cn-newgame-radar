"""将渠道远程 Icon 校验并缓存到本地，避免防盗链、过期和浏览器直链失败。"""
from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 NewGameMonitor/1.0",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


def _download_icon(url: str, icon_dir: Path) -> dict:
    now = datetime.now().astimezone().isoformat()
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        if len(response.content) > 5 * 1024 * 1024:
            raise ValueError("图片超过 5MB 限制")
        image = Image.open(BytesIO(response.content))
        image.seek(0)
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        relative = Path("remote") / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]}.webp"
        target = icon_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "WEBP", quality=88, method=4)
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


def cache_remote_icons(conn: sqlite3.Connection, icon_dir: Path, workers: int = 8) -> dict:
    icon_dir.mkdir(parents=True, exist_ok=True)
    urls = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT icon_url FROM source_items WHERE icon_url IS NOT NULL AND icon_url<>''"
        )
        if not row[0].startswith("local-icon://")
    }
    successful = {
        row[0]: row[1] for row in conn.execute(
            "SELECT source_url, relative_path FROM icon_assets WHERE status='success'"
        )
        if row[1] and (icon_dir / row[1]).exists()
    }
    pending = sorted(urls - set(successful))
    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download_icon, url, icon_dir): url for url in pending}
            for future in as_completed(futures):
                results.append(future.result())
        with conn:
            conn.executemany(
                """
                INSERT INTO icon_assets (
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
        "cached": len(successful) + sum(item["status"] == "success" for item in results),
        "downloaded": sum(item["status"] == "success" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
    }
