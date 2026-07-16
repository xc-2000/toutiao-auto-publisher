from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_profiles import ModelProfileStore
from toutiao_accounts import AccountStore, QRLoginManager


def make_config(root: Path) -> dict[str, object]:
    return {
        "dashboard": {
            "accounts_file": str(root / "accounts.json"),
            "models_file": str(root / "models.json"),
            "secret_key_file": str(root / ".secret-key"),
        },
        "toutiao": {"base_url": "https://mp.toutiao.com", "aid": 1231},
        "ai": {
            "base_url": "https://api.default.test/v1",
            "api_key": "default-article-key",
            "model": "default-article",
            "temperature": 0.3,
            "json_mode": True,
        },
        "cover": {
            "base_url": "https://api.default.test/v1",
            "api_key": "default-cover-key",
            "model": "default-cover",
            "size": "1024x1024",
            "quality": "low",
        },
        "video": {
            "base_url": "https://api.default.test/v1",
            "api_key": "default-video-key",
            "model": "default-video",
            "size": "1280x720",
            "duration": 8,
            "aspect_ratio": "16:9",
            "poll_interval": 5,
            "timeout": 900,
            "create_path": "/videos",
        },
    }


def profile(user_id: str, media_id: str, name: str) -> dict[str, object]:
    return {
        "data": {
            "is_login": True,
            "user": {"id": user_id, "nickname": f"user-{name}"},
            "media": {"id": media_id, "name": name, "description": f"intro-{name}"},
        }
    }


class AccountStoreTests(unittest.TestCase):
    def test_encrypted_multi_account_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AccountStore(make_config(root), root)
            first = store.save_account(profile("u1", "m1", "账号一"), "sessionid=COOKIE_ONE")
            second = store.save_account(
                profile("u2", "m2", "账号二"),
                {"sessionid": "COOKIE_TWO"},
                {"x-custom-token": "HEADER_SECRET"},
            )

            disk = (root / "accounts.json").read_text(encoding="utf-8")
            self.assertNotIn("COOKIE_ONE", disk)
            self.assertNotIn("COOKIE_TWO", disk)
            self.assertNotIn("HEADER_SECRET", disk)
            self.assertNotIn("secret", first)
            self.assertEqual(store.snapshot()["count"], 2)
            self.assertEqual(store.snapshot()["active_id"], second["id"])

            store.activate(first["id"])
            credentials = store.active_credentials()
            self.assertIsNotNone(credentials)
            assert credentials is not None
            self.assertEqual(credentials["cookies"]["sessionid"], "COOKIE_ONE")
            second_credentials = store.credentials(second["id"])
            self.assertIsNotNone(second_credentials)
            assert second_credentials is not None
            self.assertEqual(second_credentials["cookies"]["sessionid"], "COOKIE_TWO")
            self.assertTrue(store.delete(first["id"]))
            self.assertEqual(store.snapshot()["active_id"], second["id"])
            self.assertFalse(store.delete("missing"))


class ModelProfileStoreTests(unittest.TestCase):
    def test_encrypted_profiles_apply_edit_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            store = ModelProfileStore(config, root)
            article = store.save(
                {
                    "kind": "article",
                    "name": "文章生产模型",
                    "base_url": "https://provider.test/v1/",
                    "model": "article-v1",
                    "api_key": "ARTICLE_SECRET_KEY",
                    "temperature": 0.8,
                    "json_mode": False,
                }
            )
            cover = store.save(
                {
                    "kind": "cover",
                    "name": "封面生产模型",
                    "base_url": "https://images.test/v1",
                    "model": "cover-v1",
                    "api_key": "COVER_SECRET_KEY",
                    "size": "1536x1024",
                    "quality": "high",
                }
            )
            video = store.save(
                {
                    "kind": "video",
                    "name": "视频生产模型",
                    "base_url": "https://videos.test/v1/",
                    "model": "video-v1",
                    "api_key": "VIDEO_SECRET_KEY",
                    "size": "720x1280",
                    "duration": 12,
                    "aspect_ratio": "9:16",
                    "poll_interval": 3,
                    "timeout": 1200,
                    "create_path": "/video/generations",
                }
            )

            disk = (root / "models.json").read_text(encoding="utf-8")
            self.assertNotIn("ARTICLE_SECRET_KEY", disk)
            self.assertNotIn("COVER_SECRET_KEY", disk)
            self.assertNotIn("VIDEO_SECRET_KEY", disk)
            store.activate(article["id"])
            effective = store.apply(config)
            self.assertEqual(effective["ai"]["model"], "article-v1")
            self.assertEqual(effective["ai"]["api_key"], "ARTICLE_SECRET_KEY")
            self.assertEqual(effective["cover"]["model"], "cover-v1")
            self.assertEqual(effective["cover"]["api_key"], "COVER_SECRET_KEY")
            self.assertEqual(effective["video"]["model"], "video-v1")
            self.assertEqual(effective["video"]["api_key"], "VIDEO_SECRET_KEY")
            self.assertEqual(effective["video"]["duration"], 12)
            self.assertEqual(effective["video"]["aspect_ratio"], "9:16")
            self.assertEqual(effective["video"]["create_path"], "/video/generations")

            edited = store.save(
                {
                    "id": article["id"],
                    "kind": "article",
                    "name": "文章生产模型 2",
                    "base_url": "https://provider.test/v2",
                    "model": "article-v2",
                    "api_key": "",
                    "temperature": 0.5,
                    "json_mode": True,
                }
            )
            self.assertEqual(edited["name"], "文章生产模型 2")
            self.assertEqual(store.apply(config)["ai"]["api_key"], "ARTICLE_SECRET_KEY")
            edited_video = store.save(
                {
                    **video,
                    "name": "视频生产模型 2",
                    "api_key": "",
                    "duration": 15,
                    "timeout": 1800,
                }
            )
            self.assertEqual(edited_video["duration"], 15)
            self.assertEqual(store.apply(config)["video"]["api_key"], "VIDEO_SECRET_KEY")
            self.assertEqual(store.apply(config)["video"]["timeout"], 1800)
            with self.assertRaises(ValueError):
                store.save({**edited, "kind": "cover", "api_key": ""})

            self.assertTrue(store.delete(cover["id"]))
            self.assertEqual(store.snapshot()["active"]["cover"], "builtin-cover")
            self.assertTrue(store.delete(video["id"]))
            self.assertEqual(store.snapshot()["active"]["video"], "builtin-video")
            self.assertFalse(store.delete("builtin-article"))


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeCookie:
    def __init__(self, name: str, value: str, domain: str = ".toutiao.com") -> None:
        self.name = name
        self.value = value
        self.domain = domain


class FakeCookieStore:
    def __init__(self, cookies: list[FakeCookie] | None = None) -> None:
        self.jar = cookies or []


class FakeSession:
    def __init__(self, responses: list[FakeResponse] | None = None, cookies: list[FakeCookie] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.responses = responses or []
        self.cookies = FakeCookieStore(cookies)
        self.closed = False

    def get(self, *_: object, **__: object) -> FakeResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class QRLoginManagerTests(unittest.TestCase):
    def test_start_and_server_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = FakeSession(
                [
                    FakeResponse({"data": {"token": "TOKEN_ONE", "qrcode": "PNG_BASE64"}}),
                    FakeResponse(
                        {
                            "data": {
                                "status": 4,
                                "token": "TOKEN_TWO",
                                "qrcode": "data:image/png;base64,REFRESHED",
                            }
                        }
                    ),
                ]
            )
            manager = QRLoginManager(make_config(root), AccountStore(make_config(root), root))
            with patch("toutiao_accounts.requests.Session", return_value=session):
                started = manager.start()
            self.assertEqual(started["status"], "new")
            self.assertEqual(started["qrcode"], "data:image/png;base64,PNG_BASE64")

            refreshed = manager.poll(str(started["login_id"]))
            self.assertEqual(refreshed["status"], "new")
            self.assertEqual(refreshed["qrcode"], "data:image/png;base64,REFRESHED")

    def test_confirmed_login_fetches_profile_and_saves_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AccountStore(make_config(root), root)
            session = FakeSession(
                [
                    FakeResponse({"data": {"token": "TOKEN", "qrcode": "QR"}}),
                    FakeResponse(
                        {
                            "data": {
                                "status": 3,
                                "redirect_url": "https://mp.toutiao.com/login/callback",
                            }
                        }
                    ),
                    FakeResponse({}),
                    FakeResponse(profile("user-3", "media-3", "扫码账号")),
                ],
                [FakeCookie("sessionid", "LOGIN_COOKIE")],
            )
            manager = QRLoginManager(make_config(root), store)
            with patch("toutiao_accounts.requests.Session", return_value=session):
                started = manager.start()
            result = manager.poll(str(started["login_id"]))

            self.assertEqual(result["status"], "confirmed")
            self.assertEqual(result["account"]["name"], "扫码账号")
            self.assertEqual(store.snapshot()["count"], 1)
            credentials = store.active_credentials()
            self.assertIsNotNone(credentials)
            assert credentials is not None
            self.assertEqual(credentials["cookies"]["sessionid"], "LOGIN_COOKIE")
            self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
