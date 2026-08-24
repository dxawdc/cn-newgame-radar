import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from newgame_monitor import auth, webapp
from newgame_monitor.catalog import rebuild_catalog
from newgame_monitor.db import connect, upsert_items


class AuthApiTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            "os.environ",
            {
                "NEWGAME_BOOTSTRAP_USERNAME": "test_superadmin",
                "NEWGAME_BOOTSTRAP_PASSWORD": "test-password-123",
            },
        )
        self.environment.start()
        self.original_db = webapp.DB_PATH
        self.original_iterations = auth.PASSWORD_ITERATIONS
        webapp.DB_PATH = Path(self.temporary.name) / "auth.db"
        auth.PASSWORD_ITERATIONS = 1000
        self.client_context = TestClient(webapp.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        webapp.DB_PATH = self.original_db
        auth.PASSWORD_ITERATIONS = self.original_iterations
        self.environment.stop()
        self.temporary.cleanup()

    def login(self, username="test_superadmin", password="test-password-123"):
        response = self.client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    def seed_game(self):
        conn = connect(webapp.DB_PATH)
        upsert_items(
            conn,
            [{
                "source": "taptap", "source_item_id": "tap-auth-1",
                "name": "关注测试新游", "developer": "雷达工作室",
                "category": "角色扮演", "tags": ["剧情"],
                "gameplay_intro": "用于验证关注列表导出。",
                "event_type": "launch", "event_time": "2026-08-24",
                "detail_url": "https://example.com/game", "raw": {},
            }],
            "2026-08-24T08:00:00+08:00",
        )
        rebuild_catalog(conn)
        game_key = conn.execute("SELECT canonical_key FROM canonical_games").fetchone()[0]
        conn.close()
        return game_key

    def test_bootstrap_profile_favorite_filter_and_export(self):
        csrf = self.login()
        me = self.client.get("/api/auth/me").json()
        self.assertEqual(me["user"]["role"], "superadmin")
        game_key = self.seed_game()
        favorite = self.client.post(
            "/api/favorites", json={"game_key": game_key},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(favorite.status_code, 200, favorite.text)
        conn = connect(webapp.DB_PATH)
        rebuild_catalog(conn)
        conn.close()
        games = self.client.get("/api/games", params={"period": "all", "followed": "true"}).json()
        self.assertEqual(games["total"], 1)
        self.assertTrue(games["items"][0]["followed"])
        exported = self.client.get("/api/favorites/export.csv")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("关注测试新游", exported.content.decode("utf-8-sig"))
        profile = self.client.patch(
            "/api/account/profile",
            json={
                "display_name": "情报管理员", "current_password": "test-password-123",
                "new_password": "new-gdc-123456",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["user"]["display_name"], "情报管理员")
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        self.login(password="new-gdc-123456")

    def test_admin_can_manage_users_but_not_admins(self):
        csrf = self.login()
        admin = self.client.post(
            "/api/admin/users",
            json={
                "username": "channel_admin", "display_name": "渠道管理员",
                "password": "admin-pass-123", "role": "admin",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(admin.status_code, 200, admin.text)
        user = self.client.post(
            "/api/admin/users",
            json={
                "username": "viewer01", "display_name": "产品观察员",
                "password": "viewer-pass-123", "role": "user",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(user.status_code, 200, user.text)
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

        admin_csrf = self.login("channel_admin", "admin-pass-123")
        listed = self.client.get("/api/admin/users")
        self.assertEqual([item["role"] for item in listed.json()["items"]], ["user"])
        reset = self.client.patch(
            f"/api/admin/users/{user.json()['user']['id']}",
            json={"password": "viewer-new-123"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        forbidden = self.client.patch(
            f"/api/admin/users/{admin.json()['user']['id']}",
            json={"password": "cannot-change-123"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        self.assertEqual(forbidden.status_code, 400)

    def test_login_rate_limit_and_csrf_guard(self):
        for _ in range(5):
            response = self.client.post(
                "/api/auth/login", json={"username": "test_superadmin", "password": "wrong-password"}
            )
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post(
            "/api/auth/login",
            json={"username": "test_superadmin", "password": "test-password-123"},
        )
        self.assertEqual(blocked.status_code, 429)

        # 新测试库中使用不同账号可避免同一账号/IP 的限流键；重新清理失败记录后验证 CSRF。
        conn = connect(webapp.DB_PATH)
        conn.execute("DELETE FROM login_attempts")
        conn.commit()
        conn.close()
        csrf = self.login()
        rejected = self.client.post("/api/favorites", json={"game_key": "name:missing"})
        self.assertEqual(rejected.status_code, 403)
        self.assertTrue(csrf)

    def test_api_key_favorite_feed_and_activity_log(self):
        csrf = self.login()
        game_key = self.seed_game()
        followed = self.client.post(
            "/api/favorites", json={"game_key": game_key},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(followed.status_code, 200, followed.text)
        self.assertTrue(followed.json()["last_followed_at"])

        created = self.client.post(
            "/api/account/api-keys", json={"name": "日报同步"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 200, created.text)
        secret = created.json()["api_key"]
        key_id = created.json()["item"]["id"]
        self.assertTrue(secret.startswith("ngr_"))

        conn = connect(webapp.DB_PATH)
        stored = conn.execute("SELECT key_hash FROM user_api_keys WHERE id=?", (key_id,)).fetchone()[0]
        conn.close()
        self.assertNotEqual(stored, secret)

        feed = self.client.get(
            "/api/v1/favorites", headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(feed.status_code, 200, feed.text)
        self.assertEqual(feed.json()["total"], 1)
        self.assertEqual(feed.json()["items"][0]["key"], game_key)
        self.assertTrue(feed.json()["items"][0]["last_followed_at"])

        removed = self.client.delete(
            "/api/favorites", params={"game_key": game_key},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        logs = self.client.get("/api/account/favorite-logs").json()["items"]
        self.assertEqual([item["action"] for item in logs], ["unfollow", "follow"])

        revoked = self.client.delete(
            f"/api/account/api-keys/{key_id}", headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        denied = self.client.get(
            "/api/v1/favorites", headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(denied.status_code, 401)


if __name__ == "__main__":
    unittest.main()
