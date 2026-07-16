from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app_auth import SessionSigner, UserStore
from dashboard import UserRuntimeManager


class UserStoreTests(unittest.TestCase):
    def test_registration_login_roles_and_signed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            users = UserStore(root / "users.json")
            first, first_user = users.register("Admin", "admin-password", "管理员")
            second, second_user = users.register("writer", "writer-password", "编辑")

            self.assertTrue(first_user)
            self.assertFalse(second_user)
            self.assertEqual(first["role"], "admin")
            self.assertEqual(second["role"], "user")
            self.assertEqual(users.authenticate("ADMIN", "admin-password")["id"], first["id"])

            disk = (root / "users.json").read_text(encoding="utf-8")
            self.assertNotIn("admin-password", disk)
            self.assertNotIn("writer-password", disk)

            signer = SessionSigner(root / ".session-key", ttl_seconds=60)
            token = signer.issue(first["id"])
            self.assertEqual(signer.verify(token), first["id"])
            self.assertIsNone(signer.verify(token + "changed"))

            promoted = users.update(
                second["id"],
                actor_user_id=first["id"],
                role="admin",
                enabled=True,
            )
            self.assertEqual(promoted["role"], "admin")
            with self.assertRaises(ValueError):
                users.update(
                    first["id"],
                    actor_user_id=first["id"],
                    enabled=False,
                )


class UserRuntimeManagerTests(unittest.TestCase):
    @staticmethod
    def make_config(root: Path) -> dict[str, object]:
        return {
            "dashboard": {
                "state_file": str(root / "state" / "dashboard.json"),
                "accounts_file": str(root / "state" / "accounts.json"),
                "models_file": str(root / "state" / "models.json"),
                "secret_key_file": str(root / "state" / ".secret-key"),
            },
            "cover": {"output_dir": str(root / "covers")},
            "video": {"output_dir": str(root / "videos")},
            "upload": {"draft_dir": str(root / "drafts")},
            "toutiao": {},
        }

    def test_tenant_paths_and_legacy_files_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            state = root / "state"
            state.mkdir()
            drafts = root / "drafts"
            covers = root / "covers"
            videos = root / "videos"
            drafts.mkdir()
            covers.mkdir()
            videos.mkdir()
            article = drafts / "article.json"
            cover = covers / "cover.jpg"
            video = videos / "video.mp4"
            article.write_text("{}", encoding="utf-8")
            cover.write_bytes(b"cover")
            video.write_bytes(b"video")
            (state / "dashboard.json").write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-one",
                                "article_path": str(article),
                                "cover_path": str(cover),
                                "video_path": str(video),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (state / "accounts.json").write_text("{}", encoding="utf-8")
            (state / "models.json").write_text("{}", encoding="utf-8")
            (state / ".secret-key").write_text("key", encoding="utf-8")

            manager = UserRuntimeManager(config, root, SimpleNamespace())  # type: ignore[arg-type]
            first_config = manager.tenant_config("user-one")
            second_config = manager.tenant_config("user-two")
            self.assertNotEqual(
                first_config["dashboard"]["state_file"],
                second_config["dashboard"]["state_file"],
            )

            manager.migrate_legacy("user-one")
            migrated = json.loads(
                (state / "tenants" / "user-one" / "dashboard.json").read_text(encoding="utf-8")
            )
            job = migrated["jobs"][0]
            self.assertTrue(Path(job["article_path"]).is_file())
            self.assertTrue(Path(job["cover_path"]).is_file())
            self.assertTrue(Path(job["video_path"]).is_file())
            self.assertIn("user-one", job["article_path"])
            self.assertNotIn("user-two", job["article_path"])
            self.assertIn("user-one", job["video_path"])


if __name__ == "__main__":
    unittest.main()
