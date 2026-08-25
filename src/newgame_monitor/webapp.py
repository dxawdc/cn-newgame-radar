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
from pydantic import BaseModel

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
from .db import connect
from .gallery import extract_gallery_urls, gallery_urls_from_rows


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("NEWGAME_DB", ROOT / "data" / "newgame_monitor.db"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_DIR = Path(os.environ.get("NEWGAME_ICON_DIR", ROOT / "data" / "icons"))
SCREENSHOT_DIR = Path(os.environ.get("NEWGAME_SCREENSHOT_DIR", ROOT / "data" / "screenshots"))
BASE_PATH = "/" + os.environ.get("NEWGAME_BASE_PATH", "").strip("/") if os.environ.get("NEWGAME_BASE_PATH", "").strip("/") else ""
SESSION_COOKIE = "newgame_session"
COOKIE_PATH = BASE_PATH or "/"
COOKIE_SECURE = os.environ.get("NEWGAME_COOKIE_SECURE", "0") == "1"
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
    return connect(DB_PATH)


class LoginPayload(BaseModel):
    username: str
    password: str


class ProfilePayload(BaseModel):
    display_name: str | None = None
    current_password: str | None = None
    new_password: str | None = None


class FavoritePayload(BaseModel):
    game_key: str


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
    conn = _conn()
    try:
        return {
            row["game_key"]: row["last_followed_at"] or row["created_at"]
            for row in conn.execute(
                "SELECT game_key, created_at, last_followed_at FROM user_favorites WHERE user_id=?",
                (user_id,),
            )
        }
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
    return row["event_type"] if _iso_date(row["event_time"]) else "first_seen"


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
        relative = assets.get(remote_url)
        gallery.append({
            "url": f"{BASE_PATH}/screenshots/{relative}" if relative else remote_url,
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "cached": bool(relative),
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


def _serialize(game: dict, members: Iterable[sqlite3.Row] | None = None) -> dict:
    selected = list(members if members is not None else game["members"])
    sources = sorted(
        {row["source"] for row in game["members"]},
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
        })
    events.sort(key=lambda x: (x["date"] or "9999-12-31", x["source_label"]))
    today = date.today().isoformat()
    upcoming = [event for event in events if event["date"] >= today]
    featured = upcoming[0] if upcoming else (events[-1] if events else None)
    try:
        tags = json.loads(game["tags_json"] or "[]")
    except json.JSONDecodeError:
        tags = []
    return {
        "id": game["id"],
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
    """把渠道原子事件投影为产品聚合或渠道明细列表项。"""
    scoped = list(members)
    if view_mode == "channel":
        entries = []
        seen = set()
        for row in scoped:
            event_date, _ = _event_date(row)
            event_type = _effective_event_type(row)
            event_key = (row["source"], event_type, event_date, row["status"])
            if event_key in seen:
                continue
            seen.add(event_key)
            payload = _serialize(game, [row])
            payload.update({
                "view_mode": "channel",
                "entry_key": f'{game["canonical_key"]}|{row["source"]}|{event_type}|{event_date}',
                "source_count": 1,
                "event_sources": [_source_payload(row["source"])],
                "event_source_count": 1,
                "channel_event_count": 1,
                "later_event_count": 0,
            })
            entries.append(payload)
        return entries

    grouped: dict[str, list] = defaultdict(list)
    for row in scoped:
        grouped[_effective_event_type(row)].append(row)

    entries = []
    for event_type, rows in grouped.items():
        payload = _serialize(game, rows)
        if not payload["events"]:
            continue
        earliest_date = min(event["date"] for event in payload["events"] if event["date"])
        primary_events = [event for event in payload["events"] if event["date"] == earliest_date]
        primary_events.sort(
            key=lambda event: (
                SOURCE_ORDER.get(event["source"], len(SOURCE_ORDER)),
                event["source_label"],
            )
        )
        primary_sources = sorted(
            {event["source"] for event in primary_events},
            key=lambda source: (SOURCE_ORDER.get(source, len(SOURCE_ORDER)), SOURCE_LABELS.get(source, source)),
        )
        featured = dict(primary_events[0])
        featured["primary_source_count"] = len(primary_sources)
        scoped_sources = {event["source"] for event in payload["events"]}
        payload.update({
            "view_mode": "product",
            "entry_key": f'{game["canonical_key"]}|{event_type}|{earliest_date}',
            "featured_event": featured,
            "source_count": len(scoped_sources),
            "event_sources": [_source_payload(source) for source in primary_sources],
            "event_source_count": len(primary_sources),
            "channel_event_count": len(payload["events"]),
            "later_event_count": len(payload["events"]) - len(primary_events),
        })
        entries.append(payload)
    return entries


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
    conn = _conn()
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
        selected = []
        for row in game["members"]:
            if sources and row["source"] not in sources:
                continue
            if event_types and _effective_event_type(row) not in event_types:
                continue
            selected.append(row)
        if selected:
            # 日期筛选必须发生在聚合之后，否则范围起点会被误当作“最早渠道日期”。
            for entry in _catalog_entries_for_game(game, selected, view_mode):
                event_day = (entry.get("featured_event") or {}).get("date")
                event_date = date.fromisoformat(event_day) if event_day else None
                if start and (not event_date or event_date < start):
                    continue
                if end and (not event_date or event_date > end):
                    continue
                result.append(entry)
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
            SELECT l.game_key, l.action, l.occurred_at, g.name
            FROM favorite_activity_logs l
            LEFT JOIN canonical_games g ON g.canonical_key=l.game_key
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


@app.post("/api/favorites")
def add_favorite(payload: FavoritePayload, request: Request):
    context = _session_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM canonical_games WHERE canonical_key=?", (payload.game_key,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="游戏不存在")
        followed_at = now_iso()
        existing = conn.execute(
            "SELECT 1 FROM user_favorites WHERE user_id=? AND game_key=?",
            (context["user"]["id"], payload.game_key),
        ).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO user_favorites(user_id, game_key, created_at, last_followed_at)
                VALUES (?, ?, ?, ?)
                """,
                (context["user"]["id"], payload.game_key, followed_at, followed_at),
            )
            conn.execute(
                """
                INSERT INTO favorite_activity_logs(user_id, game_key, action, occurred_at)
                VALUES (?, ?, 'follow', ?)
                """,
                (context["user"]["id"], payload.game_key, followed_at),
            )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM user_favorites WHERE user_id=?", (context["user"]["id"],)
        ).fetchone()[0]
        current = conn.execute(
            "SELECT last_followed_at FROM user_favorites WHERE user_id=? AND game_key=?",
            (context["user"]["id"], payload.game_key),
        ).fetchone()
        return {
            "followed": True, "favorite_count": count,
            "last_followed_at": current[0] if current else followed_at,
        }
    finally:
        conn.close()


@app.delete("/api/favorites")
def remove_favorite(game_key: str, request: Request):
    context = _session_context(request)
    _require_csrf(request, context)
    conn = _conn()
    try:
        cursor = conn.execute(
            "DELETE FROM user_favorites WHERE user_id=? AND game_key=?",
            (context["user"]["id"], game_key),
        )
        if cursor.rowcount:
            conn.execute(
                """
                INSERT INTO favorite_activity_logs(user_id, game_key, action, occurred_at)
                VALUES (?, ?, 'unfollow', ?)
                """,
                (context["user"]["id"], game_key, now_iso()),
            )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM user_favorites WHERE user_id=?", (context["user"]["id"],)
        ).fetchone()[0]
        return {"followed": False, "favorite_count": count}
    finally:
        conn.close()


def _csv_safe(value) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _favorite_products(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    favorite_rows = conn.execute(
        """
        SELECT game_key, created_at, last_followed_at
        FROM user_favorites WHERE user_id=?
        ORDER BY last_followed_at DESC
        """,
        (user_id,),
    ).fetchall()
    games_by_key = {game["canonical_key"]: game for game in _load_catalog(conn)}
    items = []
    for favorite in favorite_rows:
        game = games_by_key.get(favorite["game_key"])
        if not game:
            continue
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
    conn = _conn()
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
    favorite_keys = set(favorite_dates)
    items = _filtered_games(
        period, anchor, date_from, date_to, _csv(source), _csv(category),
        _csv(developer), _event_scope(event_type), q, view,
    )
    if followed:
        items = [item for item in items if item["key"] in favorite_keys]
    for item in items:
        item["followed"] = item["key"] in favorite_keys
        item["last_followed_at"] = favorite_dates.get(item["key"])
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
    product_total = len({item["key"] for item in items})
    start = (page - 1) * page_size
    return {
        "view": view, "total": total, "product_total": product_total,
        "page": page, "page_size": page_size, "items": items[start:start + page_size],
    }


@app.get("/api/games/{game_id}")
def game_detail(game_id: int, request: Request):
    context = _session_context(request, required=False)
    favorite_dates = _favorite_dates(context["user"]["id"]) if context else {}
    conn = _conn()
    try:
        game = next((item for item in _load_catalog(conn) if item["id"] == game_id), None)
        if not game:
            return {"error": "not_found"}
        payload = _serialize(game)
        payload["latest_intro"] = _latest_game_intro(game)
        payload["followed"] = payload["key"] in favorite_dates
        payload["last_followed_at"] = favorite_dates.get(payload["key"])
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
    conn = _conn()
    try:
        last = conn.execute(
            "SELECT finished_at FROM collection_runs WHERE status='success' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        source_count = conn.execute("SELECT COUNT(DISTINCT source) FROM source_items").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0]
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
    conn = _conn()
    try:
        categories = [row[0] for row in conn.execute(
            "SELECT category FROM canonical_games WHERE category IS NOT NULL AND category<>'' GROUP BY category ORDER BY COUNT(*) DESC, category LIMIT 80"
        )]
        developers = [row[0] for row in conn.execute(
            "SELECT developer FROM canonical_games WHERE developer IS NOT NULL AND developer<>'' GROUP BY developer ORDER BY COUNT(*) DESC, developer LIMIT 100"
        )]
        present_sources = {row[0] for row in conn.execute("SELECT DISTINCT source FROM source_items")}
        present_events = {row[0] for row in conn.execute(
            """
            SELECT DISTINCT CASE
                WHEN trim(COALESCE(event_time, ''))='' THEN 'first_seen'
                ELSE event_type END
            FROM source_items
            """
        )}
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


@app.get("/api/health")
def health():
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT r.* FROM collection_runs r
            JOIN (SELECT source, MAX(id) id FROM collection_runs GROUP BY source) latest ON latest.id=r.id
            ORDER BY r.source
            """
        )
        return {
            "database": str(DB_PATH),
            "sources": [dict(row) for row in rows],
            "generated_at": datetime.now().astimezone().isoformat(),
        }
    finally:
        conn.close()
