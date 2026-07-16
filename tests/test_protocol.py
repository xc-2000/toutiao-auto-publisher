from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import patch

from toutiao_protocol import (
    ProtocolChallenge,
    ToutiaoProtocolClient,
    credential_summary,
    parse_cookie_header,
    save_credentials,
)


class FakeCookies:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, name: str, value: str, **_: object) -> None:
        self.values[name] = value


class FakeResponse:
    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "https://mp.toutiao.com/mp/agw/article/new",
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.url = url
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else "<html></html>"

    def json(self) -> object:
        if self.payload is None:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies()
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self) -> None:
        pass


class FakeMultipart:
    def __init__(self) -> None:
        self.parts: list[dict[str, object]] = []
        self.closed = False

    def addpart(self, name: str, **kwargs: object) -> None:
        self.parts.append({"name": name, **kwargs})

    def close(self) -> None:
        self.closed = True


class FakeChromeBridge:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def publish(
        self,
        path: str,
        params: dict[str, object],
        body: str,
        cookies: dict[str, str],
    ) -> dict[str, object]:
        self.calls.append(
            {"path": path, "params": params, "body": body, "cookies": cookies}
        )
        return self.response


def make_config(root: Path) -> dict[str, object]:
    return {
        "toutiao": {
            "base_url": "https://mp.toutiao.com",
            "cookie_env": "TEST_TOUTIAO_COOKIE",
            "cookie_file": "cookie.txt",
            "headers_file": "headers.json",
            "protocol_log_dir": "logs",
            "csrf_cookie": "passport_csrf_token",
            "csrf_header": "x-tt-passport-csrf-token",
        },
        "upload": {"report_dir": str(root / "reports")},
    }


class ProtocolClientTests(unittest.TestCase):
    def test_cookie_parse_and_persistence(self) -> None:
        self.assertEqual(
            parse_cookie_header("Cookie: sessionid=a=b; passport_csrf_token=csrf123"),
            {"sessionid": "a=b", "passport_csrf_token": "csrf123"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            saved = save_credentials(
                config,
                root,
                "sessionid=abc; passport_csrf_token=csrf123",
                {"x-secsdk-csrf-token": "signed"},
            )
            summary = credential_summary(config, root)
            self.assertEqual(saved["cookie_count"], 2)
            self.assertTrue(summary["configured"])
            self.assertEqual(summary["header_count"], 1)

    def test_payload_matches_graphic_publish_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"TEST_TOUTIAO_COOKIE": "sessionid=abc; passport_csrf_token=csrf123"},
            clear=False,
        ):
            root = Path(directory)
            fake = FakeSession([])
            client = ToutiaoProtocolClient(make_config(root), root, session=fake)
            article = SimpleNamespace(
                topic="测试选题",
                title="这是一个测试标题",
                summary="测试摘要",
                body_html="<p>正文内容</p>",
            )
            cover = {"url": "https://img.test/a.jpg", "uri": "tos/a", "width": 1200, "height": 800}
            draft = client.build_payload(article, "draft", cover)
            publish = client.build_payload(article, "publish", cover, activity_tag="778899")

            self.assertEqual(draft["save"], 0)
            self.assertEqual(draft["entrance"], "")
            self.assertEqual(publish["save"], 1)
            self.assertEqual(publish["entrance"], "main")
            self.assertEqual(publish["source"], 29)
            self.assertEqual(publish["article_type"], 0)
            self.assertEqual(publish["article_ad_type"], 3)
            self.assertEqual(publish["activity_tag"], 778899)
            self.assertEqual(publish["mp_editor_stat"], "{}")
            self.assertEqual(json.loads(publish["draft_form_data"]), {"coverType": 2})
            self.assertEqual(json.loads(publish["pgc_feed_covers"])[0]["uri"], "tos/a")
            self.assertEqual(publish["ic_uri_list"], ["tos/a"])
            self.assertEqual(json.loads(publish["extra"])["gd_ext"]["from_page"], "publisher_mp")
            self.assertEqual(
                json.loads(publish["search_creation_info"]),
                {"searchTopOne": False, "abstract": "测试摘要", "clue_id": ""},
            )
            self.assertEqual(fake.headers["x-tt-passport-csrf-token"], "csrf123")

    def test_publish_uses_chrome_security_bridge_and_returns_pgc_id(self) -> None:
        responses = [
            FakeResponse({"code": 0, "message": "success", "data": {"articleType": 0}}),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TEST_TOUTIAO_COOKIE": "sessionid=abc"}, clear=False
        ):
            root = Path(directory)
            fake = FakeSession(responses)
            bridge = FakeChromeBridge(
                {"code": 0, "message": "保存成功", "data": {"pgc_id": "778899"}}
            )
            client = ToutiaoProtocolClient(
                make_config(root), root, session=fake, chrome_bridge=bridge
            )
            article = SimpleNamespace(
                topic="测试选题",
                title="这是一个测试标题",
                summary="测试摘要",
                body_html="<p>正文内容</p>",
            )
            report = client.publish(article, "draft", None)

            self.assertEqual(report["pgc_id"], "778899")
            self.assertEqual(report["transport"], "http+chrome-security")
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(len(bridge.calls), 1)
            call = bridge.calls[0]
            self.assertEqual(call["path"], "/mp/agw/article/publish")
            form = parse_qs(str(call["body"]), keep_blank_values=True)
            self.assertEqual(form["save"], ["0"])
            self.assertNotIn("ic_uri_list", form)
            self.assertNotIn("_signature", call["params"])
            self.assertEqual(call["cookies"], {"sessionid": "abc"})

    def test_publish_can_use_legacy_http_transport_when_bridge_is_disabled(self) -> None:
        responses = [
            FakeResponse({"code": 0, "message": "success", "data": {"articleType": 0}}),
            FakeResponse({"code": 0, "message": "success", "data": {"pgcId": "9911"}}),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TEST_TOUTIAO_COOKIE": "sessionid=abc"}, clear=False
        ):
            root = Path(directory)
            config = make_config(root)
            config["toutiao"]["chrome_protocol_enabled"] = False
            fake = FakeSession(responses)
            client = ToutiaoProtocolClient(config, root, session=fake)
            article = SimpleNamespace(
                topic="测试选题",
                title="这是一个测试标题",
                summary="测试摘要",
                body_html="<p>正文内容</p>",
            )
            with patch.object(client, "_sign_publish_request", return_value="SIGNED_VALUE"):
                report = client.publish(article, "draft", None)

            self.assertEqual(report["pgc_id"], "9911")
            self.assertEqual(report["transport"], "http")
            _, _, kwargs = fake.calls[1]
            self.assertEqual(kwargs["params"]["_signature"], "SIGNED_VALUE")

    def test_image_upload_is_multipart(self) -> None:
        response = FakeResponse(
            {
                "code": 0,
                "data": {
                    "origin_image_url": "https://img.test/cover.jpg",
                    "origin_image_uri": "tos/cover",
                    "image_width": 1200,
                    "image_height": 800,
                    "image_mime_type": "image/jpeg",
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TEST_TOUTIAO_COOKIE": "sessionid=abc"}, clear=False
        ):
            root = Path(directory)
            image = root / "cover.jpg"
            image.write_bytes(b"jpeg-fixture")
            fake = FakeSession([response])
            client = ToutiaoProtocolClient(make_config(root), root, session=fake)
            multipart = FakeMultipart()
            with patch("toutiao_protocol.CurlMime", return_value=multipart):
                result = client.upload_image(image)

            self.assertEqual(result["uri"], "tos/cover")
            _, _, kwargs = fake.calls[0]
            self.assertIs(kwargs["multipart"], multipart)
            self.assertEqual(
                multipart.parts,
                [
                    {
                        "name": "image",
                        "content_type": "image/jpeg",
                        "filename": "cover.jpg",
                        "local_path": image,
                    }
                ],
            )
            self.assertTrue(multipart.closed)
            self.assertEqual(kwargs["params"]["upload_source"], 20020003)

    def test_challenge_code_is_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TEST_TOUTIAO_COOKIE": "sessionid=abc"}, clear=False
        ):
            root = Path(directory)
            fake = FakeSession([FakeResponse({"code": 2222, "message": "verify"})])
            client = ToutiaoProtocolClient(make_config(root), root, session=fake)
            with self.assertRaises(ProtocolChallenge) as raised:
                client.check_session()
            self.assertEqual(raised.exception.code, 2222)

    def test_protocol_log_redacts_sensitive_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"TEST_TOUTIAO_COOKIE": "sessionid=abc; passport_csrf_token=csrf123"},
            clear=False,
        ):
            root = Path(directory)
            fake = FakeSession([FakeResponse({"code": 0, "message": "success", "data": {}})])
            client = ToutiaoProtocolClient(make_config(root), root, session=fake)
            client.check_session()
            log_file = next((root / "logs").glob("*.jsonl"))
            line = log_file.read_text(encoding="utf-8")
            self.assertNotIn("csrf123", line)
            self.assertIn("<redacted>", line)

    def test_protocol_log_redacts_publish_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TEST_TOUTIAO_COOKIE": "sessionid=abc"}, clear=False
        ):
            root = Path(directory)
            fake = FakeSession([FakeResponse({"code": 0, "message": "success", "data": {}})])
            client = ToutiaoProtocolClient(make_config(root), root, session=fake)
            client._request("GET", "/test", params={"aid": 1231, "_signature": "SECRET_SIGNATURE"})
            line = next((root / "logs").glob("*.jsonl")).read_text(encoding="utf-8")
            self.assertNotIn("SECRET_SIGNATURE", line)
            self.assertIn("<redacted>", line)


if __name__ == "__main__":
    unittest.main()
