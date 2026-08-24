"""账号、密码、会话和角色权限的底层能力。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone


PASSWORD_ITERATIONS = int(os.environ.get("NEWGAME_PASSWORD_ITERATIONS", "600000"))
SESSION_DAYS = int(os.environ.get("NEWGAME_SESSION_DAYS", "30"))
LOGIN_MAX_FAILURES = int(os.environ.get("NEWGAME_LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_MINUTES = int(os.environ.get("NEWGAME_LOGIN_WINDOW_MINUTES", "15"))
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ROLES = {"superadmin", "admin", "user"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def normalize_username(username: str) -> str:
    return username.strip().casefold()


def validate_username(username: str) -> str:
    value = username.strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("账号需为 3–32 位字母、数字、点、下划线或短横线")
    return value


def validate_display_name(display_name: str) -> str:
    value = display_name.strip()
    if not 1 <= len(value) <= 40:
        raise ValueError("显示名长度需为 1–40 个字符")
    return value


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    if len(password) > 128:
        raise ValueError("密码不能超过 128 个字符")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations_text)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def public_user(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def bootstrap_superadmin(conn: sqlite3.Connection) -> dict:
    username = os.environ.get("NEWGAME_BOOTSTRAP_USERNAME", "").strip()
    password = os.environ.get("NEWGAME_BOOTSTRAP_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "首次启动前必须设置 NEWGAME_BOOTSTRAP_USERNAME 和 "
            "NEWGAME_BOOTSTRAP_PASSWORD；系统不会创建固定口令的默认账号"
        )
    normalized = normalize_username(username)
    row = conn.execute("SELECT * FROM users WHERE normalized_username=?", (normalized,)).fetchone()
    if row:
        return public_user(row)
    created_at = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO users(
            username, normalized_username, display_name, password_hash,
            role, is_active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'superadmin', 1, ?, ?)
        """,
        (
            validate_username(username), normalized, "GDC 超级管理员",
            hash_password(password), created_at, created_at,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
    return public_user(row)


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM users WHERE normalized_username=?",
        (normalize_username(username),),
    ).fetchone()
    if not row or not row["is_active"] or not verify_password(password, row["password_hash"]):
        return None
    return row


def login_attempt_key(username: str, client_ip: str) -> str:
    material = f"{normalize_username(username)}|{client_ip}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def login_allowed(conn: sqlite3.Connection, attempt_key: str) -> tuple[bool, int]:
    row = conn.execute(
        "SELECT locked_until FROM login_attempts WHERE attempt_key=?", (attempt_key,)
    ).fetchone()
    if not row or not row["locked_until"]:
        return True, 0
    locked_until = datetime.fromisoformat(row["locked_until"])
    now = datetime.now(timezone.utc)
    if locked_until <= now:
        conn.execute("DELETE FROM login_attempts WHERE attempt_key=?", (attempt_key,))
        conn.commit()
        return True, 0
    return False, max(1, int((locked_until - now).total_seconds()))


def record_login_failure(conn: sqlite3.Connection, attempt_key: str) -> None:
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT failed_count, window_started_at FROM login_attempts WHERE attempt_key=?",
        (attempt_key,),
    ).fetchone()
    window = timedelta(minutes=LOGIN_WINDOW_MINUTES)
    if not row or datetime.fromisoformat(row["window_started_at"]) + window <= now:
        failed_count = 1
        window_started = now
    else:
        failed_count = row["failed_count"] + 1
        window_started = datetime.fromisoformat(row["window_started_at"])
    locked_until = (now + window).isoformat() if failed_count >= LOGIN_MAX_FAILURES else None
    conn.execute(
        """
        INSERT INTO login_attempts(attempt_key, failed_count, window_started_at, locked_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(attempt_key) DO UPDATE SET
            failed_count=excluded.failed_count,
            window_started_at=excluded.window_started_at,
            locked_until=excluded.locked_until
        """,
        (attempt_key, failed_count, window_started.isoformat(), locked_until),
    )
    conn.commit()


def clear_login_failures(conn: sqlite3.Connection, attempt_key: str) -> None:
    conn.execute("DELETE FROM login_attempts WHERE attempt_key=?", (attempt_key,))
    conn.commit()


def create_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    csrf_token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_DAYS)
    conn.execute("DELETE FROM user_sessions WHERE expires_at<=?", (now.isoformat(),))
    conn.execute(
        """
        INSERT INTO user_sessions(token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (token_hash, user_id, csrf_token, now.isoformat(), now.isoformat(), expires.isoformat()),
    )
    conn.commit()
    return token, csrf_token, expires.isoformat()


def session_user(conn: sqlite3.Connection, token: str | None) -> tuple[sqlite3.Row, str] | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        """
        SELECT u.*, s.csrf_token, s.expires_at
        FROM user_sessions s
        JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1
        """,
        (token_hash, now),
    ).fetchone()
    if not row:
        return None
    conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE token_hash=?", (now, token_hash))
    conn.commit()
    return row, row["csrf_token"]


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (token_hash,))
    conn.commit()


def revoke_user_sessions(conn: sqlite3.Connection, user_id: int, except_token: str | None = None) -> None:
    if except_token:
        keep_hash = hashlib.sha256(except_token.encode("utf-8")).hexdigest()
        conn.execute(
            "DELETE FROM user_sessions WHERE user_id=? AND token_hash<>?",
            (user_id, keep_hash),
        )
    else:
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
    conn.commit()
