from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dashboard import AutomationEngine, JobManager, StateStore


class FakeExecutor:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, tuple[object, ...]]] = []

    def submit(self, function: object, *args: object) -> None:
        self.submissions.append((getattr(function, "__name__", ""), args))

    def shutdown(self, **_: object) -> None:
        return None


def job(
    job_id: str,
    status: str,
    article_path: Path,
    mode: str | None = None,
    *,
    content_type: str = "article",
    video_path: Path | None = None,
) -> dict[str, object]:
    return {
        "id": job_id,
        "topic": f"topic-{job_id}",
        "guidance": "",
        "word_count": 1200,
        "content_type": content_type,
        "status": status,
        "title": "测试标题",
        "summary": "",
        "tags": [],
        "article_path": str(article_path),
        "cover_path": "",
        "video_path": str(video_path or ""),
        "error": "",
        "publish_mode": mode,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


class DashboardRecoveryTests(unittest.TestCase):
    def test_challenge_acceptances_persist_and_are_isolated_by_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard.json"
            store = StateStore(path, {})

            first = store.accept_challenges(
                "account-one",
                [
                    {
                        "id": "1869000000000001",
                        "title": "图文活动",
                        "content_type": "article",
                        "biz_id": 1,
                    },
                    {
                        "id": "1869000000000002",
                        "title": "视频活动",
                        "content_type": "video",
                        "biz_id": 2,
                    },
                ],
            )
            duplicate = store.accept_challenges(
                "account-one",
                [{"id": "1869000000000001", "title": "图文活动"}],
            )
            daily = store.accept_challenges(
                "account-one",
                [
                    {
                        "id": "1869000000000004",
                        "title": "每日任务",
                        "repeat_mode": "daily",
                        "repeat_reason": "规则命中“每日”",
                    }
                ],
            )
            daily_duplicate = store.accept_challenges(
                "account-one",
                [{"id": "1869000000000004", "repeat_mode": "daily"}],
            )
            store.accept_challenges(
                "account-two",
                [{"id": "1869000000000003", "title": "另一个活动"}],
            )

            self.assertEqual(first["new_count"], 2)
            self.assertEqual(duplicate["new_count"], 0)
            self.assertEqual(daily["new_count"], 1)
            self.assertEqual(daily_duplicate["new_count"], 0)
            store.data["challenge_acceptances"]["account-one"]["1869000000000004"][
                "accepted_on"
            ] = "2000-01-01"
            self.assertNotIn(
                "1869000000000004",
                store.accepted_challenge_ids(
                    "account-one",
                    [{"id": "1869000000000004", "repeat_mode": "daily"}],
                ),
            )
            next_day = store.accept_challenges(
                "account-one",
                [{"id": "1869000000000004", "repeat_mode": "daily"}],
            )
            self.assertEqual(next_day["new_count"], 1)
            self.assertEqual(next_day["records"][0]["claim_count"], 2)
            weekly = store.accept_challenges(
                "account-one",
                [{"id": "1869000000000005", "repeat_mode": "weekly"}],
            )
            weekly_duplicate = store.accept_challenges(
                "account-one",
                [{"id": "1869000000000005", "repeat_mode": "weekly"}],
            )
            self.assertEqual(weekly["new_count"], 1)
            self.assertEqual(weekly_duplicate["new_count"], 0)
            store.data["challenge_acceptances"]["account-one"]["1869000000000005"][
                "accepted_on"
            ] = "2000-01-01"
            next_week = store.accept_challenges(
                "account-one",
                [{"id": "1869000000000005", "repeat_mode": "weekly"}],
            )
            self.assertEqual(next_week["new_count"], 1)
            self.assertEqual(next_week["records"][0]["claim_count"], 2)
            self.assertEqual(
                store.accepted_challenge_ids("account-one"),
                {
                    "1869000000000001",
                    "1869000000000002",
                    "1869000000000004",
                    "1869000000000005",
                },
            )
            self.assertEqual(
                store.accepted_challenge_ids("account-two"),
                {"1869000000000003"},
            )

            reloaded = StateStore(path, {})
            self.assertEqual(
                reloaded.accepted_challenge_ids("account-one"),
                {
                    "1869000000000001",
                    "1869000000000002",
                    "1869000000000004",
                    "1869000000000005",
                },
            )

    def test_uploads_are_recovered_before_generation_and_cover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            article_path = root / "article.json"
            article_path.write_text("{}", encoding="utf-8")
            store = StateStore(root / "dashboard.json", {})
            store.add_job(job("generation", "generating", root / "missing.json"))
            store.add_job(job("cover", "cover-generating", article_path))
            store.add_job(job("upload", "publish-queued", article_path, "draft"))
            manager = JobManager({}, root, store, SimpleNamespace(), SimpleNamespace())
            manager.executor.shutdown(wait=False, cancel_futures=True)
            fake = FakeExecutor()
            manager.executor = fake  # type: ignore[assignment]

            recovered = manager.recover_pending()

            self.assertEqual(
                fake.submissions,
                [
                    ("_publish", ("upload", "draft")),
                    ("_generate", ("generation", None)),
                    ("_resume_cover", ("cover", None)),
                ],
            )
            self.assertEqual([item["action"] for item in recovered], ["draft", "generate", "cover"])
            self.assertEqual(store.get_job("upload")["status"], "publish-queued")

    def test_invalid_upload_is_marked_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "dashboard.json", {})
            store.add_job(job("missing", "publishing", root / "missing.json", "draft"))
            manager = JobManager({}, root, store, SimpleNamespace(), SimpleNamespace())
            manager.executor.shutdown(wait=False, cancel_futures=True)
            fake = FakeExecutor()
            manager.executor = fake  # type: ignore[assignment]

            self.assertEqual(manager.recover_pending(), [])
            self.assertEqual(fake.submissions, [])
            self.assertEqual(store.get_job("missing")["status"], "error")
            self.assertEqual(store.get_job("missing")["stage"], "error")
            self.assertEqual(store.get_job("missing")["progress"], 100)

    def test_video_generation_and_upload_are_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            article_path = root / "article.json"
            video_path = root / "video.mp4"
            article_path.write_text("{}", encoding="utf-8")
            video_path.write_bytes(b"video")
            store = StateStore(root / "dashboard.json", {})
            store.add_job(
                job(
                    "video-generation",
                    "video-downloading",
                    article_path,
                    content_type="video",
                )
            )
            store.add_job(
                job(
                    "video-upload",
                    "video-processing",
                    article_path,
                    "publish",
                    content_type="video",
                    video_path=video_path,
                )
            )
            manager = JobManager({}, root, store, SimpleNamespace(), SimpleNamespace())
            manager.executor.shutdown(wait=False, cancel_futures=True)
            fake = FakeExecutor()
            manager.executor = fake  # type: ignore[assignment]

            recovered = manager.recover_pending()

            self.assertEqual(
                fake.submissions,
                [
                    ("_publish", ("video-upload", "publish")),
                    ("_resume_video", ("video-generation", None)),
                ],
            )
            self.assertEqual([item["action"] for item in recovered], ["publish", "video"])
            self.assertEqual(store.get_job("video-upload")["progress"], 84)
            self.assertEqual(store.get_job("video-generation")["stage"], "video-generating")


class FakeAccounts:
    def __init__(self) -> None:
        self.active_id = "account-two"
        self.rows = [
            {"id": "account-one", "name": "账号一", "avatar": ""},
            {"id": "account-two", "name": "账号二", "avatar": ""},
        ]

    def snapshot(self) -> dict[str, object]:
        return {"active_id": self.active_id, "accounts": self.rows, "count": 2}

    def credentials(self, account_id: str) -> dict[str, object] | None:
        return {"account": {"id": account_id}, "cookies": {}, "headers": {}} if account_id in {row["id"] for row in self.rows} else None


class FakeJobs:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def create(self, topic: str, **kwargs: object) -> None:
        self.created.append({"topic": topic, **kwargs})


class AutomationEngineTests(unittest.TestCase):
    def test_account_profiles_and_topic_filters_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(
                Path(directory) / "dashboard.json",
                {"enabled": True, "interval_minutes": 60, "mode": "draft", "pick_count": 1},
            )
            accounts = FakeAccounts()
            jobs = FakeJobs()
            engine = AutomationEngine(store, SimpleNamespace(), jobs, accounts)  # type: ignore[arg-type]

            initial = engine.status()
            first = next(item for item in initial["accounts"] if item["account_id"] == "account-one")
            second = next(item for item in initial["accounts"] if item["account_id"] == "account-two")
            self.assertFalse(first["enabled"])
            self.assertTrue(second["enabled"])

            engine.update(
                "account-one",
                True,
                30,
                "publish",
                2,
                ["科技"],
                ["csdn"],
            )
            profile = store.snapshot()["automation"]["accounts"]["account-one"]
            topics = [
                {"title": "技术热点", "category": "科技", "source": "CSDN", "source_keys": ["csdn"]},
                {"title": "社会热点", "category": "社会", "source": "头条", "source_keys": ["toutiao"]},
            ]
            engine._run_account("account-one", profile, topics)

            self.assertEqual(len(jobs.created), 1)
            self.assertEqual(jobs.created[0]["topic"], "技术热点")
            self.assertEqual(jobs.created[0]["account_id"], "account-one")
            self.assertEqual(jobs.created[0]["auto_action"], "publish")


if __name__ == "__main__":
    unittest.main()
