from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chrome_protocol_bridge import ChromeProtocolBridge
from toutiao_publisher import Article, PublisherError
from toutiao_video import ToutiaoVideoClient
from video_generation import VideoGenerator


class FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self.payload


def make_client(root: Path) -> ToutiaoVideoClient:
    return ToutiaoVideoClient(
        {
            "toutiao": {
                "base_url": "https://mp.toutiao.com",
                "user_agent": "test-agent",
                "video_request_timeout_seconds": 10,
            }
        },
        root,
        cookie_override={"sessionid": "cookie"},
    )


class ToutiaoVideoTests(unittest.TestCase):
    def test_aws4_vod_signature_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = make_client(Path(directory))
            token = {
                "AccessKeyId": "AKID",
                "SecretAccessKey": "SECRET",
                "SessionToken": "TOKEN",
            }
            with patch("toutiao_video.requests.request", return_value=FakeResponse({})) as request:
                client._vod_request("GET", {"Action": "ApplyUploadInner", "Version": "1"}, b"", token)
            headers = request.call_args.kwargs["headers"]
            self.assertTrue(headers["Authorization"].startswith("AWS4-HMAC-SHA256 "))
            self.assertIn("/cn-north-1/vod/aws4_request", headers["Authorization"])
            self.assertIn("SignedHeaders=host;x-amz-date;x-amz-security-token", headers["Authorization"])
            self.assertIn("X-Amz-Date", headers)
            self.assertEqual(headers["x-amz-security-token"], "TOKEN")
            client.close()

    def test_apply_commit_and_binary_response_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = make_client(Path(directory))
            node = {
                "UploadHost": "upload.test",
                "SessionKey": "SESSION",
                "Vid": "fallback-vid",
                "StoreInfos": [{"StoreUri": "store/path", "Auth": "AUTH"}],
            }
            parsed = client._upload_node(
                {"Result": {"InnerUploadAddress": {"UploadNodes": [node]}}}
            )
            self.assertEqual(parsed["SessionKey"], "SESSION")
            self.assertEqual(
                client._committed_vid({"Result": {"Results": [{"Vid": "committed-vid"}]}}),
                "committed-vid",
            )
            for payload in ({"success": 0}, {"code": 2000}):
                with patch("toutiao_video.requests.post", return_value=FakeResponse(payload)):
                    client._upload_binary(node, b"video-bytes")
            with patch(
                "toutiao_video.requests.post",
                return_value=FakeResponse({"success": 1, "message": "failed"}),
            ):
                with self.assertRaises(PublisherError):
                    client._upload_binary(node, b"video-bytes")
            client.close()

    def test_publish_payload_for_draft_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = make_client(Path(directory))
            article = Article(
                topic="热点",
                title="测试视频标题",
                summary="测试摘要",
                body_markdown="正文" * 100,
                tags=["科技", "热点"],
                generated_at="2026-01-01T00:00:00+00:00",
            )
            meta = {"Width": 1280, "Height": 720, "Duration": 13.8}
            poster = {"URI": "poster-uri", "URL": "https://poster.test/image.jpg"}
            draft = client.build_publish_payload(article, "video.mp4", "vid", meta, poster, "draft")
            publish = client.build_publish_payload(
                article, "video.mp4", "vid", meta, poster, "publish", activity_tag="778899"
            )
            self.assertEqual(draft["PublishType"], 0)
            self.assertEqual(publish["PublishType"], 1)
            self.assertEqual(publish["VideoInfo"]["Vid"], "vid")
            self.assertEqual(publish["VideoInfo"]["Duration"], 13)
            self.assertEqual(publish["VideoType"], 3)
            self.assertEqual(publish["ActivityTag"], 778899)
            self.assertTrue(publish["ComplianceInfo"]["IsAIGC"])
            client.close()

    def test_chrome_json_bridge_primes_dynamic_csrf(self) -> None:
        source = inspect.getsource(ChromeProtocolBridge._publish_locked)
        self.assertIn("/xigua/api/upload/GetPublishAuth", source)
        self.assertIn("xigua_csrf_token", source)
        self.assertIn("x-csrf-token", source)

    def test_video_size_follows_per_job_aspect_ratio(self) -> None:
        generator = object.__new__(VideoGenerator)
        generator.video = {"size": "1280x720", "aspect_ratio": "16:9"}
        self.assertEqual(generator._size_for_ratio("16:9"), "1280x720")
        self.assertEqual(generator._size_for_ratio("9:16"), "720x1280")
        self.assertEqual(generator._size_for_ratio("1:1"), "1024x1024")

    def test_grok_video_payload_and_poll_path(self) -> None:
        generator = object.__new__(VideoGenerator)
        generator.video = {}
        generator.model = "grok-imagine-video"
        generator.create_path = "/videos/generations"
        article = Article(
            topic="technology",
            title="Video protocol test",
            summary="Verify the configured video model request.",
            body_markdown="Body",
            tags=["test"],
            generated_at="2026-07-13T00:00:00+00:00",
        )

        payload = generator._create_payload(article, "documentary", 8, "16:9", "1280x720")

        self.assertEqual(payload["model"], "grok-imagine-video")
        self.assertEqual(payload["duration"], 8)
        self.assertEqual(payload["resolution"], "720p")
        self.assertNotIn("seconds", payload)
        self.assertNotIn("size", payload)
        self.assertEqual(generator._poll_path("request-123"), "/videos/request-123")

    def test_openai_video_payload_remains_compatible(self) -> None:
        generator = object.__new__(VideoGenerator)
        generator.video = {"duration_field": "seconds"}
        generator.model = "sora-2"
        generator.create_path = "/videos"
        article = Article(
            topic="technology",
            title="Video protocol test",
            summary="Verify the configured video model request.",
            body_markdown="Body",
            tags=["test"],
            generated_at="2026-07-13T00:00:00+00:00",
        )

        payload = generator._create_payload(article, "documentary", 8, "16:9", "1280x720")

        self.assertEqual(payload["seconds"], "8")
        self.assertEqual(payload["size"], "1280x720")
        self.assertNotIn("duration", payload)
        self.assertNotIn("resolution", payload)
        self.assertEqual(generator._poll_path("video-123"), "/videos/video-123")


if __name__ == "__main__":
    unittest.main()
