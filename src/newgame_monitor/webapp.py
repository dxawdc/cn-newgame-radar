"""新游聚合看板的本地查询 API。"""
from __future__ import annotations

import json
import csv
import hashlib
import io
import mimetypes
import os
import re
import secrets
import sqlite3
from urllib.parse import quote, urlsplit
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    ROLES,
    authenticate,
    bootstrap_superadmin,
    clear_login_failures,
    create_session,
    delete_session,
    hash_password,
    login_allowed,
    login_attempt_key,
    normalize_username,
    now_iso,
    public_user,
    record_login_failure,
    revoke_user_sessions,
    session_user,
    validate_display_name,
    validate_password,
    validate_username,
    verify_password,
)
from .catalog import (
    EVENT_LABELS, SOURCE_LABELS, SOURCE_NOTES, SOURCE_ORDER, SOURCE_QUALITY,
    rebuild_catalog,
)
from .db import connect, connect_readonly
from .gallery import extract_gallery_urls, gallery_urls_from_rows
from .phase2_model import (
    CONTROLLED_EVENT_TYPES, audit_phase2_model, finish_review_job,
    resolve_game_uuid,
)


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("NEWGAME_DB", ROOT / "data" / "newgame_monitor.db"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_DIR = Path(os.environ.get("NEWGAME_ICON_DIR", ROOT / "data" / "icons"))
SCREENSHOT_DIR = Path(os.environ.get("NEWGAME_SCREENSHOT_DIR", ROOT / "data" / "screenshots"))
BASE_PATH = "/" + os.environ.get("NEWGAME_BASE_PATH", "").strip("/") if os.environ.get("NEWGAME_BASE_PATH", "").strip("/") else ""
SESSION_COOKIE = "newgame_session"
COOKIE_PATH = BASE_PATH or "/"
COOKIE_SECURE = os.environ.get("NEWGAME_COOKIE_SECURE", "0") == "1"
GAME_ID_REDIRECT_MAX_HOPS = 16
ICON_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
mimetypes.add_type("image/webp", ".webp")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    conn = connect(DB_PATH)
    try:
        bootstrap_superadmin(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="新游雷达", version="1.0.0", lifespan=_lifespan)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
app.mount("/icons", StaticFiles(directory=ICON_DIR), name="icons")
app.mount("/screenshots", StaticFiles(directory=SCREENSHOT_DIR), name="screenshots")


APP_ONLY_DETAIL_SOURCES = {
    "honor_gamecenter", "oppo_gamecenter", "vivo_gamecenter",
}


def _conn() -> sqlite3.Connection:
    # Schema 已在 lifespan 中迁移；请求连接只做业务读写，禁止重复 DDL。
    return connect(DB_PATH, migrate=False)


def _read_conn() -> sqlite3.Connection:
    """为高频纯查询路径提供 OS 级只读连接。"""
    return connect_readonly(DB_PATH)


class LoginPayload(BaseModel):
    username: str
    password: str


class ProfilePayload(BaseModel):
    display_name: str | None = None
    current_password: str | None = None
    new_password: str | None = None


class FavoritePayload(BaseModel):
    game_uuid: str | None = None
    game_key: str | None = None


class ApiKeyCreatePayload(BaseModel):
    name: str = "默认密钥"


class UserCreatePayload(BaseModel):
    username: str
    display_name: str
    password: str
    role: str = "user"


class UserUpdatePayload(BaseModel):
    username: str | None = None
    display_name: str | None = None
    password: str | None = None
    role: str | None = None
    is_active: bool | None = None


class ReviewUpdatePayload(BaseModel):
    status: str
    result: dict = Field(default_factory=dict)
    next_retry_at: str | None = None


def _session_context(request: Request, *, required: bool = True) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    conn = _conn()
    try:
        result = session_user(conn, token)
    finally:
        conn.close()
    if not result:
        if required:
            raise HTTPException(status_code=401, detail="请先登录")
        return None
    row, csrf_token = result
    return {"user": public_user(row), "csrf_token": csrf_token, "token": token}


def _require_csrf(request: Request, context: dict) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or supplied != context["csrf_token"]:
        raise HTTPException(status_code=403, detail="请求校验失败，请刷新页面后重试")


def _permissions(user: dict) -> dict:
    return {
        "manage_users": user["role"] in {"superadmin", "admin"},
        "manage_admins": user["role"] == "superadmin",
    }


def _favorite_dates(user_id: int) -> dict[str, str]:
    conn = _read_conn()
    try:
        result: dict[str, str] = {}
        for row in conn.execute(
            """
            SELECT game_uuid,created_at,last_followed_at
            FROM user_favorite_games WHERE user_id=?
            """,
            (user_id,),
        ):
            current_uuid = resolve_game_uuid(conn, row["game_uuid"])
            if current_uuid:
                followed_at = row["last_followed_at"] or row["created_at"]
                result[current_uuid] = max(result.get(current_uuid, ""), followed_at)
        # 兼容仍由旧客户端或迁移前脚本写入的名称键收藏。
        for row in conn.execute(
            """
            SELECT g.game_uuid,f.created_at,f.last_followed_at
            FROM user_favorites f
            JOIN canonical_games g ON g.canonical_key=f.game_key
            WHERE f.user_id=?
            """,
            (user_id,),
        ):
            followed_at = row["last_followed_at"] or row["created_at"]
            result[row["game_uuid"]] = max(result.get(row["game_uuid"], ""), followed_at)
        return result
    finally:
        conn.close()


def _favorite_keys(user_id: int) -> set[str]:
    return set(_favorite_dates(user_id))


def _public_detail_url(source: str, value: str | None) -> str | None:
    """只把浏览器可直接访问的商店页暴露给前端。"""
    if source in APP_ONLY_DETAIL_SOURCES or not value:
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _api_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _api_key_user(request: Request) -> dict:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="请通过 Authorization: Bearer <API_KEY> 提供密钥")
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT k.id AS api_key_id, u.*
            FROM user_api_keys k JOIN users u ON u.id=k.user_id
            WHERE k.key_hash=? AND k.revoked_at IS NULL AND u.is_active=1
            """,
            (_api_key_hash(token),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="API Key 无效或已撤销")
        conn.execute(
            "UPDATE user_api_keys SET last_used_at=? WHERE id=?",
            (now_iso(), row["api_key_id"]),
        )
        conn.commit()
        return public_user(row)
    finally:
        conn.close()


def _iso_date(value: str | None) -> str:
    if value and len(value) >= 10:
        candidate = value[:10]
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    return ""


def _event_date(row: sqlite3.Row) -> tuple[str, str]:
    scheduled = _iso_date(row["event_time"])
    if scheduled:
        return scheduled, "scheduled"
    return _iso_date(row["first_seen_at"]), "discovered"


def _effective_event_type(row: sqlite3.Row) -> str:
    """没有明确事件日期时，只能表示首次采集发现，不能冒充原事件已发生。"""
    if not _iso_date(row["event_time"]):
        return "first_seen"
    raw_type = row["event_type"]
    if raw_type in EVENT_LABELS:
        return raw_type
    controlled = CONTROLLED_EVENT_TYPES.get(raw_type, "unknown")
    return {
        "test": "beta", "test_recruitment": "recruiting_beta",
        "listing": "new_listing",
    }.get(controlled, controlled)


def _csv(values: list[str] | None) -> set[str]:
    result = set()
    for value in values or []:
        result.update(item.strip() for item in value.split(",") if item.strip())
    return result


def _event_scope(values: list[str] | None) -> set[str]:
    """API 默认排除仅表示采集时间、没有明确业务日期的事件。"""
    if values is None:
        return set(EVENT_LABELS) - {"first_seen"}
    return _csv(values)


def _period_range(period: str, anchor: str | None) -> tuple[date | None, date | None]:
    if period == "all":
        return None, None
    current = date.fromisoformat(anchor) if anchor else date.today()
    if period == "day":
        return current, current
    if period == "week":
        start = current - timedelta(days=current.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        start = current.replace(day=1)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, next_month - timedelta(days=1)
    raise ValueError("period 仅支持 day/week/month/all")


def _load_catalog(conn: sqlite3.Connection) -> list[dict]:
    if conn.execute("SELECT COUNT(*) FROM canonical_games").fetchone()[0] == 0:
        rebuild_catalog(conn)
    games = {row["id"]: {**dict(row), "members": []} for row in conn.execute("SELECT * FROM canonical_games")}
    rows = conn.execute(
        """
        SELECT cm.game_id, si.*
        FROM canonical_members cm
        JOIN source_items si ON si.id=cm.source_row_id
        ORDER BY si.event_time, si.first_seen_at
        """
    )
    for row in rows:
        if row["game_id"] in games:
            games[row["game_id"]]["members"].append(row)
    assets = {
        row["source_url"]: row["relative_path"]
        for row in conn.execute("SELECT source_url, relative_path FROM icon_assets WHERE status='success'")
        if row["relative_path"] and (ICON_DIR / row["relative_path"]).exists()
    }
    screenshot_assets = {
        row["source_url"]: row["relative_path"]
        for row in conn.execute(
            "SELECT source_url, relative_path FROM screenshot_assets WHERE status='success'"
        )
        if row["relative_path"] and (SCREENSHOT_DIR / row["relative_path"]).exists()
    }
    for game in games.values():
        game["_screenshot_assets"] = screenshot_assets
        candidates = [game.get("icon_url"), *[row["icon_url"] for row in game["members"]]]
        for candidate in dict.fromkeys(value for value in candidates if value):
            if candidate.startswith("local-icon://"):
                relative = candidate.removeprefix("local-icon://")
            else:
                relative = assets.get(candidate)
            if relative and (ICON_DIR / relative).exists():
                game["_local_icon_url"] = f"{BASE_PATH}/icons/{relative}"
                break
    return list(games.values())


def _resolve_game_id(
    conn: sqlite3.Connection,
    requested_id: int,
    *,
    existing_ids: set[int] | None = None,
) -> int | None:
    """把已删除的旧产品 ID 安全解析到当前产品，遇到断链、环或超长链则失败。"""
    if existing_ids is None:
        existing_ids = {
            row["id"] for row in conn.execute("SELECT id FROM canonical_games")
        }
    current_id = requested_id
    seen: set[int] = set()
    for hop in range(GAME_ID_REDIRECT_MAX_HOPS + 1):
        if current_id in seen:
            return None
        seen.add(current_id)
        if current_id in existing_ids:
            return current_id
        if hop == GAME_ID_REDIRECT_MAX_HOPS:
            return None
        redirect = conn.execute(
            "SELECT new_game_id FROM canonical_game_id_redirects WHERE old_game_id=?",
            (current_id,),
        ).fetchone()
        if not redirect:
            return None
        try:
            current_id = int(redirect["new_game_id"])
        except (TypeError, ValueError):
            return None
    return None


def _game_gallery(game: dict) -> list[dict]:
    def media_count(row) -> int:
        try:
            raw_json = row["raw_json"]
        except (KeyError, IndexError):
            raw_json = {}
        return len(extract_gallery_urls(row["source"], raw_json))

    ordered_rows = sorted(
        game["members"],
        key=lambda row: (
            -min(5, media_count(row)),
            -SOURCE_QUALITY.get(row["source"], 0),
            SOURCE_ORDER.get(row["source"], len(SOURCE_ORDER)),
        ),
    )
    assets = game.get("_screenshot_assets") or {}
    gallery = []
    for source, remote_url in gallery_urls_from_rows(ordered_rows):
        local_prefix = "local-screenshot://"
        if remote_url.startswith(local_prefix):
            relative = remote_url.removeprefix(local_prefix)
            local_exists = (ICON_DIR / relative).exists()
            url = f"{BASE_PATH}/icons/{relative}" if local_exists else None
        else:
            relative = assets.get(remote_url)
            local_exists = bool(relative)
            url = f"{BASE_PATH}/screenshots/{relative}" if relative else remote_url
        if not url:
            continue
        gallery.append({
            "url": url,
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "cached": local_exists,
        })
        if len(gallery) == 5:
            break
    return gallery


def _source_payload(source: str) -> dict:
    return {
        "key": source,
        "label": SOURCE_LABELS.get(source, source),
        "note": SOURCE_NOTES.get(source),
    }


def _row_value(row, key: str, default=""):
    if isinstance(row, dict):
        return row.get(key, default)
    return row[key] if key in row.keys() else default


def _latest_event_rows(members: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    """同一产品按来源渠道和原始事件类型仅保留最近观测，允许档期调整。"""
    latest = {}
    for row in members:
        # 业务主键使用采集器给出的原始事件类型。空日期只是该事件尚未定档，
        # 不能因此拆成 first_seen 与原事件两条产品记录。
        key = (row["source"], row["event_type"])
        rank = (
            _row_value(row, "last_seen_at"),
            _row_value(row, "id", 0),
        )
        current = latest.get(key)
        if current is None or rank >= current[0]:
            latest[key] = (rank, row)
    return [item[1] for item in latest.values()]


def _serialize(game: dict, members: Iterable[sqlite3.Row] | None = None) -> dict:
    member_rows = list(members) if members is not None else list(game["members"])
    selected = _latest_event_rows(member_rows)
    sources = sorted(
        {row["source"] for row in member_rows},
        key=lambda source: (SOURCE_ORDER.get(source, len(SOURCE_ORDER)), SOURCE_LABELS.get(source, source)),
    )
    events = []
    seen_events = set()
    for row in selected:
        event_date, precision = _event_date(row)
        event_type = _effective_event_type(row)
        key = (row["source"], event_type, event_date, row["status"])
        if key in seen_events:
            continue
        seen_events.add(key)
        events.append({
            "source": row["source"],
            "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
            "source_note": SOURCE_NOTES.get(row["source"]),
            "type": event_type,
            "type_label": EVENT_LABELS.get(event_type, event_type),
            "date": event_date,
            "date_precision": precision,
            "end_date": _iso_date(row["event_end_time"]),
            "status": row["status"],
            "detail_url": _public_detail_url(row["source"], row["detail_url"]),
            "observed_at": _row_value(row, "last_seen_at"),
        })
    events.sort(key=lambda x: (x["date"] or "9999-12-31", x["source_label"]))
    today = date.today().isoformat()
    upcoming = [event for event in events if event["date"] >= today]
    if upcoming:
        featured_date = min(event["date"] for event in upcoming)
        featured = max(
            (event for event in upcoming if event["date"] == featured_date),
            key=lambda event: (event["observed_at"], -SOURCE_ORDER.get(event["source"], 999)),
        )
    elif events:
        featured_date = max(event["date"] for event in events)
        featured = max(
            (event for event in events if event["date"] == featured_date),
            key=lambda event: (event["observed_at"], -SOURCE_ORDER.get(event["source"], 999)),
        )
    else:
        featured = None
    try:
        tags = json.loads(game["tags_json"] or "[]")
    except json.JSONDecodeError:
        tags = []
    game_uuid = game.get("game_uuid") or game["canonical_key"]
    return {
        "id": game["id"],
        "uuid": game_uuid,
        "game_uuid": game_uuid,
        "key": game["canonical_key"],
        "name": game["name"],
        "developer": game["developer"],
        "category": game["category"],
        "tags": tags[:8],
        "intro": game["gameplay_intro"],
        "icon_url": game.get("_local_icon_url") or game["icon_url"],
        "rating": game["rating"],
        "first_seen_at": game["first_seen_at"],
        "last_seen_at": game["last_seen_at"],
        "source_count": len(sources),
        "event_count": len(events),
        "sources": [_source_payload(source) for source in sources],
        "events": events,
        "featured_event": featured,
        "gallery": _game_gallery(game),
    }


def _catalog_entries_for_game(game: dict, members: Iterable[sqlite3.Row], view_mode: str) -> list[dict]:
    """按产品或“产品＋渠道”聚合，事件类型仅作为组内轨迹。"""
    scoped = _latest_event_rows(members)
    if view_mode == "channel":
        entries = []
        grouped: dict[str, list] = defaultdict(list)
        for row in scoped:
            grouped[row["source"]].append(row)
        for source, rows in sorted(
            grouped.items(),
            key=lambda item: (
                SOURCE_ORDER.get(item[0], len(SOURCE_ORDER)),
                SOURCE_LABELS.get(item[0], item[0]),
            ),
        ):
            payload = _serialize(game, rows)
            if not payload["events"]:
                continue
            payload.update({
                "view_mode": "channel",
                "group_key": f'{game["canonical_key"]}|{source}',
                "entry_key": f'{game["canonical_key"]}|{source}',
                "source_count": 1,
                "event_sources": [_source_payload(source)],
                "event_source_count": 1,
                "channel_event_count": len(payload["events"]),
                "later_event_count": max(0, len(payload["events"]) - 1),
            })
            entries.append(payload)
        return entries

    payload = _serialize(game, scoped)
    if not payload["events"]:
        return []
    featured = dict(payload["featured_event"])
    featured_date = featured["date"]
    primary_sources = sorted(
        {event["source"] for event in payload["events"] if event["date"] == featured_date},
        key=lambda source: (
            SOURCE_ORDER.get(source, len(SOURCE_ORDER)),
            SOURCE_LABELS.get(source, source),
        ),
    )
    all_sources = sorted(
        {event["source"] for event in payload["events"]},
        key=lambda source: (
            SOURCE_ORDER.get(source, len(SOURCE_ORDER)),
            SOURCE_LABELS.get(source, source),
        ),
    )
    featured["primary_source_count"] = len(primary_sources)
    payload.update({
        "view_mode": "product",
        "group_key": game["canonical_key"],
        "entry_key": game["canonical_key"],
        "featured_event": featured,
        "source_count": len(all_sources),
        "event_sources": [_source_payload(source) for source in all_sources],
        "event_source_count": len(all_sources),
        "channel_event_count": len(payload["events"]),
        "later_event_count": max(0, len(payload["events"]) - 1),
    })
    return [payload]


_EVENT_ONLY_INTRO = re.compile(
    r"(?:\d{1,2}:\d{2}|\d{1,2}月\d{1,2}日|正式上线|公测开启|预下载|"
    r"开测|测试开启|限量测试|删档测试|不删档测试|开启预约)"
)


def _latest_game_intro(game: dict) -> dict | None:
    """返回最近采集到的有效产品介绍，排除纯事件通知。"""
    full_candidates = []
    candidates = []
    for row in game["members"]:
        full_text = re.sub(r"\s*\n\s*", "\n", row["full_description"] or "").strip()
        if full_text:
            full_candidates.append((len(full_text), row["last_seen_at"], row, full_text))
        text = re.sub(r"\s+", " ", row["gameplay_intro"] or "").strip()
        status = re.sub(r"\s+", " ", row["status"] or "").strip()
        if not text or text == status:
            continue
        if len(text) <= 80 and _EVENT_ONLY_INTRO.search(text):
            continue
        candidates.append((row["last_seen_at"], len(text), row, text))
    if full_candidates:
        _, _, row, text = max(full_candidates, key=lambda item: (item[0], item[1]))
        return {
            "text": text,
            "kind": "full",
            "source": row["source"],
            "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
            "collected_at": row["last_seen_at"],
            "detail_url": _public_detail_url(row["source"], row["detail_url"]),
        }
    if not candidates:
        fallback = re.sub(r"\s+", " ", game.get("gameplay_intro") or "").strip()
        return {"text": fallback, "kind": "summary", "source": None, "source_label": "聚合资料", "collected_at": None, "detail_url": None} if fallback else None
    _, _, row, text = max(candidates, key=lambda item: (item[0], item[1]))
    return {
        "text": text,
        "kind": "summary",
        "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "collected_at": row["last_seen_at"],
        "detail_url": _public_detail_url(row["source"], row["detail_url"]),
    }


def _filtered_games(
    period: str = "all", anchor: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    sources: set[str] | None = None, categories: set[str] | None = None,
    developers: set[str] | None = None, event_types: set[str] | None = None,
    q: str | None = None, view_mode: str = "product",
) -> list[dict]:
    start, end = _period_range(period, anchor)
    if date_from:
        start = date.fromisoformat(date_from)
    if date_to:
        end = date.fromisoformat(date_to)
    conn = _read_conn()
    try:
        games = _load_catalog(conn)
    finally:
        conn.close()
    result = []
    needle = (q or "").casefold().strip()
    for game in games:
        if needle and needle not in " ".join([
            game["name"] or "", game["developer"] or "", game["category"] or "",
            game["gameplay_intro"] or "", game["tags_json"] or "",
        ]).casefold():
            continue
        if categories and (game["category"] or "") not in categories:
            continue
        if developers and (game["developer"] or "") not in developers:
            continue
        source_scoped = [
            row for row in game["members"]
            if not sources or row["source"] in sources
        ]
        # 必须先按原始事件主键选出当前版本，再投影/筛选 first_seen；否则显式
        # 查询 first_seen 会把已经被定档记录取代的旧空日期版本重新捞出来。
        selected = [
            row for row in _latest_event_rows(source_scoped)
            if not event_types or _effective_event_type(row) in event_types
        ]
        if selected:
            # 先按日期确定命中的事件，再按产品或“产品＋渠道”聚合。事件类型只
            # 用于筛选和轨迹展示，不再决定列表项数量。
            date_scoped = []
            for row in selected:
                event_day, _ = _event_date(row)
                event_date = date.fromisoformat(event_day) if event_day else None
                if start and (not event_date or event_date < start):
                    continue
                if end and (not event_date or event_date > end):
                    continue
                date_scoped.append(row)
            if date_scoped:
                result.extend(_catalog_entries_for_game(game, date_scoped, view_mode))
    return result


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/auth/login")
def login(payload: LoginPayload, response: Response, request: Request):
    conn = _conn()
    try:
        bootstrap_superadmin(conn)
        client_ip = request.client.host if request.client else "unknown"
        attempt_key = login_attempt_key(payload.username, client_ip)
        allowed, retry_after = login_allowed(conn, attempt_key)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"登录尝试过多，请在 {max(1, retry_after // 60 + 1)} 分钟后重试",
                headers={"Retry-After": str(retry_after)},
            )
        row = authenticate(conn, payload.username, payload.password)
        if not row:
            record_login_failure(conn, attempt_key)
            raise HTTPException(status_code=401, detail="账号或密码错误")
        clear_login_failures(conn, attempt_key)
        token, csrf_token, expires_at = create_session(conn, row["id"])
        user = public_user(row)
    finally:
        conn.close()
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", path=COOKIE_PATH, max_age=30 * 24 * 60 * 60,
    )
    return {
        "user": user, "csrf_token": csrf_token,
        "expires_at": expires_at, "permissions": _permissions(user),
    }


@app.get("/api/auth/me")
def me(request: Request):
    context = _session_context(request)
    conn = _conn()
    try:
        favorite_count = conn.execute(
            "SELECT COUNT(*) FROM user_favorites WHERE user_id=?",
            (context["user"]["id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "user": context["user"], "csrf_token": context["csrf_token"],
        "favorite_count": favorite_count, "permissions": _permissions(context["user"]),
    }


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    context = _session_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        delete_session(conn, context["token"])
    finally:
        conn.close()
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH)
    return {"status": "ok"}


@app.patch("/api/account/profile")
def update_profile(payload: ProfilePayload, request: Request):
    context = _session_context(request)
    _require_csrf(request, context)
    if payload.display_name is None and payload.new_password is None:
        raise HTTPException(status_code=400, detail="没有需要保存的修改")
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (context["user"]["id"],)).fetchone()
        fields, values = [], []
        if payload.display_name is not None:
            try:
                display_name = validate_display_name(payload.display_name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            fields.append("display_name=?")
            values.append(display_name)
        if payload.new_password is not None:
            if not payload.current_password or not verify_password(payload.current_password, row["password_hash"]):
                raise HTTPException(status_code=400, detail="当前密码不正确")
            try:
                validate_password(payload.new_password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            fields.append("password_hash=?")
            values.append(hash_password(payload.new_password))
        fields.append("updated_at=?")
        values.append(now_iso())
        values.append(row["id"])
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
        if payload.new_password is not None:
            revoke_user_sessions(conn, row["id"], except_token=context["token"])
        updated = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        return {"user": public_user(updated), "permissions": _permissions(public_user(updated))}
    finally:
        conn.close()


def _api_key_item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "prefix": row["key_prefix"],
        "created_at": row["created_at"], "last_used_at": row["last_used_at"],
    }


@app.get("/api/account/api-keys")
def list_api_keys(request: Request):
    context = _session_context(request)
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM user_api_keys
            WHERE user_id=? AND revoked_at IS NULL
            ORDER BY created_at DESC
            """,
            (context["user"]["id"],),
        ).fetchall()
        return {"items": [_api_key_item(row) for row in rows]}
    finally:
        conn.close()


@app.post("/api/account/api-keys")
def create_api_key(payload: ApiKeyCreatePayload, request: Request):
    context = _session_context(request)
    _require_csrf(request, context)
    name = re.sub(r"\s+", " ", payload.name or "").strip()
    if not 1 <= len(name) <= 40:
        raise HTTPException(status_code=400, detail="密钥名称需为 1—40 个字符")
    conn = _conn()
    try:
        active_count = conn.execute(
            "SELECT COUNT(*) FROM user_api_keys WHERE user_id=? AND revoked_at IS NULL",
            (context["user"]["id"],),
        ).fetchone()[0]
        if active_count >= 10:
            raise HTTPException(status_code=400, detail="每个账号最多保留 10 个有效 API Key")
        secret = "ngr_" + secrets.token_urlsafe(32)
        created_at = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO user_api_keys(user_id, name, key_prefix, key_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                context["user"]["id"], name, secret[:12],
                _api_key_hash(secret), created_at,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_api_keys WHERE id=?", (cursor.lastrowid,)).fetchone()
        return {"api_key": secret, "item": _api_key_item(row)}
    finally:
        conn.close()


@app.delete("/api/account/api-keys/{key_id}")
def revoke_api_key(key_id: int, request: Request):
    context = _session_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            UPDATE user_api_keys SET revoked_at=?
            WHERE id=? AND user_id=? AND revoked_at IS NULL
            """,
            (now_iso(), key_id, context["user"]["id"]),
        )
        conn.commit()
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="API Key 不存在或已撤销")
        return {"status": "revoked"}
    finally:
        conn.close()


@app.get("/api/account/favorite-logs")
def favorite_logs(request: Request, limit: int = Query(default=50, ge=1, le=200)):
    context = _session_context(request)
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(r.new_key,l.game_key) AS game_key,
              l.game_key AS original_game_key, l.action, l.occurred_at, g.name
            FROM favorite_activity_logs l
            LEFT JOIN canonical_key_redirects r ON r.old_key=l.game_key
            LEFT JOIN canonical_games g
              ON g.canonical_key=COALESCE(r.new_key,l.game_key)
            WHERE l.user_id=? ORDER BY l.occurred_at DESC, l.id DESC LIMIT ?
            """,
            (context["user"]["id"], limit),
        ).fetchall()
        return {"items": [dict(row) for row in rows]}
    finally:
        conn.close()


def _admin_context(request: Request) -> dict:
    context = _session_context(request)
    if context["user"]["role"] not in {"superadmin", "admin"}:
        raise HTTPException(status_code=403, detail="当前账号没有用户管理权限")
    return context


@app.get("/api/admin/reviews")
def list_reviews(
    request: Request, queue_type: str | None = None, status: str = "pending",
    limit: int = Query(default=100, ge=1, le=200),
):
    _admin_context(request)
    if queue_type and queue_type not in {"detail", "gallery", "identity"}:
        raise HTTPException(status_code=422, detail="queue_type 不受支持")
    if status not in {"pending", "processing", "retry", "resolved", "dismissed", "all"}:
        raise HTTPException(status_code=422, detail="status 不受支持")
    clauses, values = [], []
    if queue_type:
        clauses.append("q.queue_type=?")
        values.append(queue_type)
    if status != "all":
        clauses.append("q.status=?")
        values.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn = _read_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT q.*,g.name,pl.source,pl.source_item_id
            FROM review_queue q
            LEFT JOIN canonical_games g ON g.game_uuid=q.game_uuid
            LEFT JOIN platform_listings pl ON pl.id=q.listing_id
            {where}
            ORDER BY q.priority DESC,q.updated_at,q.id LIMIT ?
            """,
            (*values, limit),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for field in ("evidence_json", "result_json"):
                try:
                    item[field.removesuffix("_json")] = json.loads(item.pop(field) or "{}")
                except json.JSONDecodeError:
                    item[field.removesuffix("_json")] = {}
            items.append(item)
        return {"total": len(items), "items": items}
    finally:
        conn.close()


@app.patch("/api/admin/reviews/{review_id}")
def update_review(review_id: int, payload: ReviewUpdatePayload, request: Request):
    context = _admin_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        if not conn.execute("SELECT 1 FROM review_queue WHERE id=?", (review_id,)).fetchone():
            raise HTTPException(status_code=404, detail="复核任务不存在")
        try:
            finish_review_job(
                conn, review_id, status=payload.status, result=payload.result,
                next_retry_at=payload.next_retry_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        conn.commit()
        return {"status": payload.status, "review_id": review_id}
    finally:
        conn.close()


@app.get("/api/internal/model-v2/health")
def model_v2_health(request: Request):
    _admin_context(request)
    conn = _read_conn()
    try:
        return audit_phase2_model(conn)
    finally:
        conn.close()


def _assert_manageable(actor: dict, target: sqlite3.Row) -> None:
    if actor["id"] == target["id"]:
        raise HTTPException(status_code=400, detail="请在个人中心修改当前账号")
    if actor["role"] == "admin" and target["role"] != "user":
        raise HTTPException(status_code=403, detail="管理员只能管理普通用户")


def _is_last_superadmin(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(
        row and row["role"] == "superadmin"
        and conn.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND is_active=1").fetchone()[0] <= 1
    )


@app.get("/api/admin/users")
def list_users(request: Request):
    context = _admin_context(request)
    conn = _conn()
    try:
        if context["user"]["role"] == "superadmin":
            rows = conn.execute("SELECT * FROM users ORDER BY role, id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM users WHERE role='user' ORDER BY id").fetchall()
        return {"items": [public_user(row) for row in rows]}
    finally:
        conn.close()


@app.post("/api/admin/users")
def create_user(payload: UserCreatePayload, request: Request):
    context = _admin_context(request)
    _require_csrf(request, context)
    role = payload.role
    if role not in ROLES:
        raise HTTPException(status_code=400, detail="账号角色无效")
    if context["user"]["role"] == "admin" and role != "user":
        raise HTTPException(status_code=403, detail="管理员只能新增普通用户")
    try:
        username = validate_username(payload.username)
        display_name = validate_display_name(payload.display_name)
        validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = _conn()
    try:
        created_at = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO users(
                username, normalized_username, display_name, password_hash,
                role, is_active, created_at, updated_at, created_by
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                username, normalize_username(username), display_name, hash_password(payload.password),
                role, created_at, created_at, context["user"]["id"],
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
        return {"user": public_user(row)}
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="该登录账号已存在") from exc
    finally:
        conn.close()


@app.patch("/api/admin/users/{user_id}")
def update_user(user_id: int, payload: UserUpdatePayload, request: Request):
    context = _admin_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="账号不存在")
        _assert_manageable(context["user"], target)
        fields, values = [], []
        if payload.username is not None:
            try:
                username = validate_username(payload.username)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            fields.extend(["username=?", "normalized_username=?"])
            values.extend([username, normalize_username(username)])
        if payload.display_name is not None:
            try:
                fields.append("display_name=?")
                values.append(validate_display_name(payload.display_name))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if payload.password is not None:
            try:
                validate_password(payload.password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            fields.append("password_hash=?")
            values.append(hash_password(payload.password))
        if payload.role is not None:
            if payload.role not in ROLES:
                raise HTTPException(status_code=400, detail="账号角色无效")
            if context["user"]["role"] != "superadmin":
                raise HTTPException(status_code=403, detail="只有超级管理员可以修改角色")
            if payload.role != "superadmin" and _is_last_superadmin(conn, user_id):
                raise HTTPException(status_code=400, detail="不能降级最后一个超级管理员")
            fields.append("role=?")
            values.append(payload.role)
        if payload.is_active is not None:
            if not payload.is_active and _is_last_superadmin(conn, user_id):
                raise HTTPException(status_code=400, detail="不能停用最后一个超级管理员")
            fields.append("is_active=?")
            values.append(int(payload.is_active))
        if not fields:
            raise HTTPException(status_code=400, detail="没有需要保存的修改")
        fields.append("updated_at=?")
        values.append(now_iso())
        values.append(user_id)
        try:
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="该登录账号已存在") from exc
        if payload.password is not None or payload.is_active is False:
            revoke_user_sessions(conn, user_id)
        updated = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return {"user": public_user(updated)}
    finally:
        conn.close()


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, request: Request):
    context = _admin_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="账号不存在")
        _assert_manageable(context["user"], target)
        if _is_last_superadmin(conn, user_id):
            raise HTTPException(status_code=400, detail="不能删除最后一个超级管理员")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        conn.close()


def _favorite_game(
    conn: sqlite3.Connection, *, game_uuid: str | None = None,
    game_key: str | None = None,
) -> sqlite3.Row | None:
    if game_uuid:
        current_uuid = resolve_game_uuid(conn, game_uuid)
        if current_uuid:
            return conn.execute(
                "SELECT * FROM canonical_games WHERE game_uuid=?", (current_uuid,)
            ).fetchone()
    current_key = (game_key or "").strip()
    for _ in range(16):
        if not current_key:
            break
        row = conn.execute(
            "SELECT * FROM canonical_games WHERE canonical_key=?", (current_key,)
        ).fetchone()
        if row:
            return row
        redirect = conn.execute(
            "SELECT new_key FROM canonical_key_redirects WHERE old_key=?", (current_key,)
        ).fetchone()
        if not redirect or redirect[0] == current_key:
            break
        current_key = redirect[0]
    return None


@app.post("/api/favorites")
def add_favorite(payload: FavoritePayload, request: Request):
    context = _session_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        game = _favorite_game(
            conn, game_uuid=payload.game_uuid, game_key=payload.game_key,
        )
        if not game:
            raise HTTPException(status_code=404, detail="游戏不存在")
        game_key = game["canonical_key"]
        game_uuid = game["game_uuid"]
        followed_at = now_iso()
        existing = conn.execute(
            "SELECT 1 FROM user_favorite_games WHERE user_id=? AND game_uuid=?",
            (context["user"]["id"], game_uuid),
        ).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO user_favorite_games(
                  user_id,game_uuid,legacy_game_key,created_at,last_followed_at
                ) VALUES (?,?,?,?,?)
                """,
                (context["user"]["id"], game_uuid, game_key, followed_at, followed_at),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO user_favorites(
                  user_id, game_key, created_at, last_followed_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (context["user"]["id"], game_key, followed_at, followed_at),
            )
            cursor = conn.execute(
                """
                INSERT INTO favorite_activity_logs(user_id, game_key, action, occurred_at)
                VALUES (?, ?, 'follow', ?)
                """,
                (context["user"]["id"], game_key, followed_at),
            )
            conn.execute(
                "INSERT INTO favorite_activity_game_ids(activity_log_id,game_uuid) VALUES (?,?)",
                (cursor.lastrowid, game_uuid),
            )
        conn.commit()
        current = conn.execute(
            "SELECT last_followed_at FROM user_favorite_games WHERE user_id=? AND game_uuid=?",
            (context["user"]["id"], game_uuid),
        ).fetchone()
        return {
            "followed": True,
            "favorite_count": len(_favorite_dates(context["user"]["id"])),
            "game_uuid": game_uuid,
            "last_followed_at": current[0] if current else followed_at,
        }
    finally:
        conn.close()


@app.delete("/api/favorites")
def remove_favorite(
    request: Request, game_uuid: str | None = None, game_key: str | None = None,
):
    context = _session_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        game = _favorite_game(conn, game_uuid=game_uuid, game_key=game_key)
        if not game:
            raise HTTPException(status_code=404, detail="游戏不存在")
        current_uuid = game["game_uuid"]
        current_key = game["canonical_key"]
        uuid_family = {current_uuid, (game_uuid or "").strip().lower()}
        for row in conn.execute("SELECT old_game_uuid FROM canonical_game_uuid_redirects"):
            if resolve_game_uuid(conn, row[0]) == current_uuid:
                uuid_family.add(row[0])
        placeholders = ",".join("?" for _ in uuid_family)
        stable_cursor = conn.execute(
            f"DELETE FROM user_favorite_games WHERE user_id=? AND game_uuid IN ({placeholders})",
            (context["user"]["id"], *sorted(uuid_family)),
        )
        legacy_cursor = conn.execute(
            "DELETE FROM user_favorites WHERE user_id=? AND game_key=?",
            (context["user"]["id"], current_key),
        )
        if stable_cursor.rowcount or legacy_cursor.rowcount:
            cursor = conn.execute(
                """
                INSERT INTO favorite_activity_logs(user_id, game_key, action, occurred_at)
                VALUES (?, ?, 'unfollow', ?)
                """,
                (context["user"]["id"], current_key, now_iso()),
            )
            conn.execute(
                "INSERT INTO favorite_activity_game_ids(activity_log_id,game_uuid) VALUES (?,?)",
                (cursor.lastrowid, current_uuid),
            )
        conn.commit()
        return {
            "followed": False,
            "favorite_count": len(_favorite_dates(context["user"]["id"])),
            "game_uuid": current_uuid,
        }
    finally:
        conn.close()


def _csv_safe(value) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _favorite_products(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    favorite_rows = list(conn.execute(
        """
        SELECT game_uuid,legacy_game_key AS game_key,created_at,last_followed_at
        FROM user_favorite_games WHERE user_id=?
        ORDER BY last_followed_at DESC
        """,
        (user_id,),
    ))
    catalog = _load_catalog(conn)
    games_by_uuid = {game["game_uuid"]: game for game in catalog}
    stable_keys = {
        resolve_game_uuid(conn, row["game_uuid"]) for row in favorite_rows
    }
    for row in conn.execute(
        """
        SELECT g.game_uuid,f.game_key,f.created_at,f.last_followed_at
        FROM user_favorites f
        JOIN canonical_games g ON g.canonical_key=f.game_key
        WHERE f.user_id=? ORDER BY f.last_followed_at DESC
        """,
        (user_id,),
    ):
        if row["game_uuid"] not in stable_keys:
            favorite_rows.append(row)
    items = []
    emitted: set[str] = set()
    for favorite in favorite_rows:
        current_uuid = resolve_game_uuid(conn, favorite["game_uuid"])
        game = games_by_uuid.get(current_uuid)
        if not game or current_uuid in emitted:
            continue
        emitted.add(current_uuid)
        payload = _serialize(game)
        payload["latest_intro"] = _latest_game_intro(game)
        payload["followed"] = True
        payload["first_followed_at"] = favorite["created_at"]
        payload["last_followed_at"] = favorite["last_followed_at"] or favorite["created_at"]
        payload["external_links"] = [
            {
                "source": row["source"],
                "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
                "url": url,
            }
            for row in game["members"]
            if (url := _public_detail_url(row["source"], row["detail_url"]))
        ]
        payload["external_links"] = list({item["url"]: item for item in payload["external_links"]}.values())
        items.append(payload)
    return items


@app.get("/api/favorites/export.csv")
def export_favorites(request: Request):
    context = _session_context(request)
    conn = _conn()
    try:
        favorite_products = _favorite_products(conn, context["user"]["id"])
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow([
            "游戏名称", "开发商", "品类", "玩法/介绍", "来源渠道", "最新事件",
            "事件日期", "事件状态", "来源链接", "首次采集发现", "最近采集",
            "首次关注时间", "最近一次关注时间",
        ])
        for payload in favorite_products:
            latest_intro = payload["latest_intro"]
            event = payload["featured_event"] or {}
            links = " | ".join(item["url"] for item in payload["external_links"])
            writer.writerow([
                _csv_safe(payload["name"]), _csv_safe(payload["developer"]),
                _csv_safe(payload["category"]), _csv_safe((latest_intro or {}).get("text") or payload["intro"]),
                _csv_safe("、".join(source["label"] for source in payload["sources"])),
                _csv_safe(event.get("type_label")), _csv_safe(event.get("date")),
                _csv_safe(event.get("status")), _csv_safe(links),
                _csv_safe(payload["first_seen_at"]), _csv_safe(payload["last_seen_at"]),
                _csv_safe(payload["first_followed_at"]), _csv_safe(payload["last_followed_at"]),
            ])
    finally:
        conn.close()
    filename = quote(f"关注的新游-{date.today().isoformat()}.csv")
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@app.get("/api/v1/favorites")
def api_favorites(request: Request):
    """使用账号生成的 Bearer API Key 获取该账号当前关注的产品。"""
    user = _api_key_user(request)
    conn = _read_conn()
    try:
        items = _favorite_products(conn, user["id"])
    finally:
        conn.close()
    return {
        "generated_at": now_iso(), "account": user["username"],
        "total": len(items), "items": items,
    }


@app.get("/api/games")
def games(
    request: Request,
    period: str = "all", anchor: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    source: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    developer: list[str] | None = Query(default=None),
    event_type: list[str] | None = Query(default=None),
    q: str | None = None, sort: str = "event_desc", view: str = "product",
    followed: bool = False,
    page: int = Query(default=1, ge=1), page_size: int = Query(default=24, ge=1, le=100),
):
    if view not in {"product", "channel"}:
        raise HTTPException(status_code=422, detail="view 仅支持 product/channel")
    context = _session_context(request, required=False)
    if followed and not context:
        raise HTTPException(status_code=401, detail="登录后才能查看已关注列表")
    favorite_dates = _favorite_dates(context["user"]["id"]) if context else {}
    favorite_uuids = set(favorite_dates)
    items = _filtered_games(
        period, anchor, date_from, date_to, _csv(source), _csv(category),
        _csv(developer), _event_scope(event_type), q, view,
    )
    if followed:
        items = [item for item in items if item["uuid"] in favorite_uuids]
    for item in items:
        item["followed"] = item["uuid"] in favorite_uuids
        item["last_followed_at"] = favorite_dates.get(item["uuid"])
    reverse = sort not in {"event_asc", "name_asc"}
    if sort == "name_asc":
        items.sort(key=lambda x: x["name"])
    elif sort == "sources_desc":
        items.sort(key=lambda x: (x["source_count"], x["name"]), reverse=True)
    else:
        items.sort(
            key=lambda x: ((x["featured_event"] or {}).get("date") or "", x["last_seen_at"]),
            reverse=reverse,
        )
    total = len(items)
    product_total = len({item["uuid"] for item in items})
    start = (page - 1) * page_size
    return {
        "view": view, "total": total, "product_total": product_total,
        "page": page, "page_size": page_size, "items": items[start:start + page_size],
    }


@app.get("/api/games/{game_id}")
def game_detail(game_id: int, request: Request):
    context = _session_context(request, required=False)
    favorite_dates = _favorite_dates(context["user"]["id"]) if context else {}
    conn = _read_conn()
    try:
        catalog = _load_catalog(conn)
        games_by_id = {item["id"]: item for item in catalog}
        resolved_id = _resolve_game_id(conn, game_id, existing_ids=set(games_by_id))
        game = games_by_id.get(resolved_id) if resolved_id is not None else None
        if not game:
            return {"error": "not_found"}
        payload = _serialize(game)
        payload["latest_intro"] = _latest_game_intro(game)
        payload["followed"] = payload["uuid"] in favorite_dates
        payload["last_followed_at"] = favorite_dates.get(payload["uuid"])
        return payload
    finally:
        conn.close()


@app.get("/api/v2/games/{game_uuid}")
def game_detail_by_uuid(game_uuid: str, request: Request):
    """稳定产品 ID 详情接口；合并后的旧 UUID 会安全跟随重定向。"""
    context = _session_context(request, required=False)
    favorite_dates = _favorite_dates(context["user"]["id"]) if context else {}
    conn = _read_conn()
    try:
        current_uuid = resolve_game_uuid(conn, game_uuid)
        if not current_uuid:
            return {"error": "not_found"}
        game = next(
            (item for item in _load_catalog(conn) if item["game_uuid"] == current_uuid),
            None,
        )
        if not game:
            return {"error": "not_found"}
        payload = _serialize(game)
        payload["latest_intro"] = _latest_game_intro(game)
        payload["followed"] = current_uuid in favorite_dates
        payload["last_followed_at"] = favorite_dates.get(current_uuid)
        return payload
    finally:
        conn.close()


@app.get("/api/summary")
def summary(
    anchor: str | None = None, view: str = "product",
    event_type: list[str] | None = Query(default=None),
):
    if view not in {"product", "channel"}:
        raise HTTPException(status_code=422, detail="view 仅支持 product/channel")
    current = anchor or date.today().isoformat()
    periods = {
        period: _filtered_games(
            period, current, event_types=_event_scope(event_type), view_mode=view,
        )
        for period in ("day", "week", "month", "all")
    }
    event_count = len(_filtered_games(
        "all", event_types=set(EVENT_LABELS), view_mode="channel",
    ))
    conn = _read_conn()
    try:
        last = conn.execute(
            "SELECT finished_at FROM collection_runs WHERE status='success' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        source_count = conn.execute("SELECT COUNT(DISTINCT source) FROM source_items").fetchone()[0]
    finally:
        conn.close()
    return {
        "anchor": current,
        "view": view,
        "today": len(periods["day"]), "week": len(periods["week"]),
        "month": len(periods["month"]), "all": len(periods["all"]),
        "events": event_count, "sources": source_count,
        "last_success_at": last[0] if last else None,
    }


@app.get("/api/filters")
def filters():
    conn = _read_conn()
    try:
        categories = [row[0] for row in conn.execute(
            "SELECT category FROM canonical_games WHERE category IS NOT NULL AND category<>'' GROUP BY category ORDER BY COUNT(*) DESC, category LIMIT 80"
        )]
        developers = [row[0] for row in conn.execute(
            "SELECT developer FROM canonical_games WHERE developer IS NOT NULL AND developer<>'' GROUP BY developer ORDER BY COUNT(*) DESC, developer LIMIT 100"
        )]
        present_sources = {row[0] for row in conn.execute("SELECT DISTINCT source FROM source_items")}
        present_events = {
            _effective_event_type(row)
            for row in conn.execute("SELECT event_type,event_time FROM source_items")
        }
    finally:
        conn.close()
    return {
        "sources": [
            {"key": key, "label": label, "note": SOURCE_NOTES.get(key)}
            for key, label in SOURCE_LABELS.items()
            if key in present_sources
        ],
        "categories": categories,
        "developers": developers,
        "event_types": [
            {
                "key": key, "label": EVENT_LABELS.get(key, key),
                "default_included": key != "first_seen",
                "note": "默认排除" if key == "first_seen" else None,
            }
            for key in EVENT_LABELS if key in present_events
        ],
    }


@app.get("/api/calendar")
def calendar(
    start: str | None = None, days: int = Query(default=42, ge=7, le=120),
    view: str = "product",
    source: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    developer: list[str] | None = Query(default=None),
    event_type: list[str] | None = Query(default=None),
    q: str | None = None,
):
    if view not in {"product", "channel"}:
        raise HTTPException(status_code=422, detail="view 仅支持 product/channel")
    first = date.fromisoformat(start) if start else date.today() - timedelta(days=7)
    last = first + timedelta(days=days - 1)
    games = _filtered_games(
        "all", date_from=first.isoformat(), date_to=last.isoformat(),
        sources=_csv(source), categories=_csv(category), developers=_csv(developer),
        event_types=_event_scope(event_type), q=q, view_mode=view,
    )
    counts, event_counts = Counter(), Counter()
    for game in games:
        event = game.get("featured_event")
        if event and first.isoformat() <= event["date"] <= last.isoformat():
            counts[event["date"]] += 1
            event_counts[(event["date"], event["type"])] += 1
    return {
        "start": first.isoformat(), "end": last.isoformat(), "view": view,
        "days": [
            {
                "date": (first + timedelta(days=offset)).isoformat(),
                "count": counts[(first + timedelta(days=offset)).isoformat()],
                "types": {
                    key: event_counts[((first + timedelta(days=offset)).isoformat(), key)]
                    for key in EVENT_LABELS
                    if event_counts[((first + timedelta(days=offset)).isoformat(), key)]
                },
            }
            for offset in range(days)
        ],
    }


def _health_snapshot(*, include_errors: bool = False) -> dict:
    conn = _read_conn()
    try:
        rows = list(conn.execute(
            """
            SELECT r.* FROM collection_runs r
            JOIN (SELECT source, MAX(id) id FROM collection_runs GROUP BY source) latest ON latest.id=r.id
            ORDER BY r.source
            """
        ))
        sources = []
        for row in rows:
            payload = {
                "source": row["source"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "status": row["status"],
                "item_count": row["item_count"],
            }
            if "metrics_json" in row.keys():
                try:
                    metrics = json.loads(row["metrics_json"] or "{}")
                except json.JSONDecodeError:
                    metrics = {}
                if metrics:
                    payload["metrics"] = metrics
            if include_errors and row["error"]:
                payload["error"] = row["error"]
            sources.append(payload)
        pipeline_row = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        pipeline = None
        if pipeline_row:
            pipeline = {
                key: pipeline_row[key]
                for key in ("run_id", "started_at", "finished_at", "status", "bundle_id")
            }
            if include_errors:
                pipeline["stages"] = [
                    dict(row) for row in conn.execute(
                        """
                        SELECT stage,source,started_at,finished_at,status,detail_json
                        FROM pipeline_stages WHERE run_id=? ORDER BY id
                        """,
                        (pipeline_row["run_id"],),
                    )
                ]
        return {
            "status": "ok",
            "sources": sources,
            "pipeline": pipeline,
            "active_quarantine": conn.execute(
                "SELECT COUNT(*) FROM catalog_quarantine WHERE status='active'"
            ).fetchone()[0],
            "generated_at": datetime.now().astimezone().isoformat(),
        }
    finally:
        conn.close()


@app.get("/livez")
def livez():
    """进程存活检查，不依赖数据库或外部渠道。"""
    return {"status": "alive", "generated_at": datetime.now().astimezone().isoformat()}


@app.get("/readyz")
def readyz(response: Response):
    """数据就绪检查：必需渠道缺失、失败或过期时返回 503。"""
    try:
        snapshot = _health_snapshot()
    except (OSError, sqlite3.Error) as exc:
        response.status_code = 503
        return {"status": "not_ready", "reasons": [f"database: {type(exc).__name__}"]}
    required = {
        item.strip() for item in os.environ.get(
            "NEWGAME_REQUIRED_SOURCES", "taptap,huawei-cache,honor-ui,oppo-ui"
        ).split(",") if item.strip()
    }
    max_age_hours = float(os.environ.get("NEWGAME_READY_MAX_AGE_HOURS", "36"))
    cutoff = datetime.now().astimezone() - timedelta(hours=max_age_hours)
    by_source = {item["source"]: item for item in snapshot["sources"]}
    reasons = []
    degraded = []
    for source in sorted(required):
        item = by_source.get(source)
        if not item:
            reasons.append(f"{source}: missing")
            continue
        if item["status"] == "degraded":
            degraded.append(source)
        elif item["status"] != "success":
            reasons.append(f'{source}: {item["status"]}')
        timestamp = item.get("finished_at") or item.get("started_at")
        try:
            observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if observed < cutoff:
                reasons.append(f"{source}: stale")
        except (AttributeError, ValueError):
            reasons.append(f"{source}: invalid_timestamp")
    if reasons:
        response.status_code = 503
    return {
        "status": "not_ready" if reasons else ("ready_degraded" if degraded else "ready"),
        "reasons": reasons,
        "degraded_sources": degraded,
        "generated_at": snapshot["generated_at"],
    }


@app.get("/api/health")
def health():
    """公开健康摘要：不暴露数据库路径和详细错误。"""
    return _health_snapshot()


@app.get("/api/internal/health")
def internal_health(request: Request):
    context = _session_context(request)
    if context["user"]["role"] not in {"superadmin", "admin"}:
        raise HTTPException(status_code=403, detail="当前账号没有运维详情权限")
    return _health_snapshot(include_errors=True)
