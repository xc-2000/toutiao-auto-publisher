from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from toutiao_challenges import (
    ToutiaoChallengeClient,
    activity_reward_value,
    clean_ocr_rule_text,
    detect_repeat_mode,
    html_to_text,
    magic_rule_image_urls,
    parse_magic_activity_page,
    score_activity_for_topic,
)


class FakeProtocol:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.session = SimpleNamespace(headers={})
        self.base_url = "https://mp.toutiao.com"

    def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        return self.responses[path]


class ChallengeProtocolTests(unittest.TestCase):
    def test_ocr_cleanup_keeps_rule_lines(self) -> None:
        text = """火. En
        回应评论任务
        我最羡慕的就是老师
        每周可参与1次
        累计互动2周，可瓜分5000元现金
        奖励将统一发放"""

        cleaned = clean_ocr_rule_text(text)

        self.assertNotIn("火. En", cleaned)
        self.assertNotIn("我最羡慕", cleaned)
        self.assertIn("每周可参与1次", cleaned)
        self.assertIn("累计互动2周", cleaned)

    def test_magic_page_extracts_daily_stages_rewards_and_topics(self) -> None:
        html = """<!doctype html><html><head><title>每日创作任务</title>
        <meta name="description" content="发布观点赢现金"></head><body><script>
        window.__MAGIC__.data={page:{"taskNameList":[{"label":"每日任务"}],
        "activityType":["graph","thread"],"topicList":[{"name":"推荐话题一"}],
        "taskConfigList":[{"activity_id":"7788","activity_start_time":1780588800,
        "activity_end_time":1780761599,"rule_data":{"rule":[
        {"start_time":1780588800,"end_time":1780675199,"config":[
        {"custom_task_name":"讨论推荐话题","target_num":1,"group_type":"weitoutiao,tuwen","award_content":"300"}]},
        {"start_time":1780675200,"end_time":1780761599,"config":[
        {"custom_task_name":"讨论推荐话题","target_num":1,"group_type":"weitoutiao,tuwen","award_content":"300"}]}
        ]}}]}};</script></body></html>"""

        result = parse_magic_activity_page(html, "7788")

        self.assertEqual(result["repeat_mode"], "daily")
        self.assertIn("2 个按日阶段", result["repeat_reason"])
        blocks = {item["title"]: item["text"] for item in result["blocks"]}
        self.assertEqual(blocks["活动介绍"], "发布观点赢现金")
        self.assertIn("图文", blocks["投稿类型"])
        self.assertIn("现金奖励 3 元", blocks["任务要求与奖励"])
        self.assertIn("推荐话题一", blocks["推荐话题"])

    def test_repeat_mode_requires_explicit_cycle_language(self) -> None:
        self.assertEqual(detect_repeat_mode("每日投稿可领现金")[0], "daily")
        self.assertEqual(detect_repeat_mode("带话题发视频，天天分现金")[0], "daily")
        self.assertEqual(detect_repeat_mode("每人仅限投稿一次")[0], "once")
        self.assertEqual(detect_repeat_mode("每周可参与1次")[0], "weekly")
        self.assertEqual(detect_repeat_mode("一键生成每日报告，打卡分奖金")[0], "unknown")
        self.assertEqual(detect_repeat_mode("围绕车型体验创作")[0], "unknown")

    def test_activity_ranking_combines_topic_match_and_reward(self) -> None:
        topic = {"title": "高考志愿怎么选", "category": "教育"}
        matching = {
            "title": "2026高考志愿分享",
            "introduction": "围绕高考志愿和填报经验创作",
            "category": "教育",
            "max_award": 30000,
            "reward_label": "3万元",
        }
        unrelated = {
            "title": "夏日汽车体验",
            "introduction": "分享汽车出行内容",
            "category": "汽车",
            "max_award": 50000,
            "reward_label": "5万元",
        }

        self.assertEqual(activity_reward_value(matching), 30000)
        self.assertGreater(
            score_activity_for_topic(topic, matching),
            score_activity_for_topic(topic, unrelated),
        )

    def test_magic_page_exposes_rule_images_when_text_config_is_absent(self) -> None:
        html = """<html><head><title>图片规则活动</title></head><body><script>
        window.__MAGIC__.data={page:{"imgSrc":"//p3-magic.byteimg.com/rule-1.webp"}};
        </script></body></html>"""

        result = parse_magic_activity_page(html, "7788")

        self.assertEqual(result["blocks"], [])
        self.assertEqual(
            result["rule_images"],
            ["https://p3-magic.byteimg.com/rule-1.webp"],
        )

    def test_rule_images_support_multiple_magic_cdn_hosts(self) -> None:
        html = """{"imgSrc":"//p3-magic.byteimg.com/one.webp",
        "imgSrc":"https://sf3-cdn-tos.toutiaostatic.com/two.png",
        "imgSrc":"https://lf3-cdn-tos.douyinstatic.com/three.jpg"}"""

        self.assertEqual(len(magic_rule_image_urls(html)), 3)

    def test_list_normalizes_video_tasks_and_protocol_filters(self) -> None:
        protocol = FakeProtocol(
            {
                ToutiaoChallengeClient.LIST_PATH: {
                    "code": 0,
                    "data": {
                        "total_num": 1,
                        "activity_list": [
                            {
                                "activity_id": 12345,
                                "title": "视频创作企划",
                                "introduction": "围绕主题发布视频",
                                "status": 2,
                                "part_in": 1,
                                "part_num": 12000,
                                "max_award": 50000,
                                "activity_reward": "5万元",
                                "activity_participants": "1.2万人",
                                "activity_time": "2026-07-01 ~ 2026-07-31",
                                "href": "https://api.toutiaoapi.com/magic/activity",
                                "fresh": 1,
                            }
                        ],
                    },
                },
                ToutiaoChallengeClient.CATEGORY_PATH: {
                    "code": 0,
                    "data": ["全部", "科技"],
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ToutiaoChallengeClient({}, Path(directory), protocol=protocol)
            result = client.list(
                biz_id=2,
                part_status=1,
                category="科技",
                query="企划",
                page=2,
                page_size=100,
            )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["categories"], ["全部", "科技"])
        activity = result["activities"][0]
        self.assertEqual(activity["id"], "12345")
        self.assertEqual(activity["content_type"], "video")
        self.assertTrue(activity["participated"])
        self.assertEqual(activity["reward_label"], "5万元")
        self.assertEqual(activity["detail_url"], "https://api.toutiaoapi.com/magic/activity")
        list_params = protocol.calls[0][2]["params"]
        self.assertEqual(list_params["act_status"], ToutiaoChallengeClient.ACTIVE_STATUS)
        self.assertEqual(list_params["biz_id"], 2)
        self.assertEqual(list_params["enter_from_mp"], 3)
        self.assertEqual(list_params["offset"], 100)
        self.assertEqual(list_params["limit"], 100)
        category_params = protocol.calls[1][2]["params"]
        self.assertEqual(
            category_params["act_status"],
            ToutiaoChallengeClient.ACTIVE_STATUS,
        )

    def test_detail_extracts_rules_and_publish_contracts(self) -> None:
        protocol = FakeProtocol(
            {
                ToutiaoChallengeClient.DETAIL_PATH: {
                    "code": 0,
                    "data": {
                        "title": "汽车征文活动",
                        "status": 2,
                        "banner": "https://img.test/banner.jpg",
                        "activity_type": {
                            "graph": {
                                "id": 7788,
                                "forum_id": 9911,
                                "label": "发表文章",
                            },
                            "video": {
                                "id": 7788,
                                "forum_id": 9911,
                                "label": "发表视频",
                            },
                        },
                        "text_block": [
                            {
                                "title": "内容要求",
                                "content": "<p>围绕车型体验创作。</p><li>主题内容不少于全文 50%</li>",
                            },
                            {"title": "法律声明", "content": "<p>声明正文</p>"},
                        ],
                    },
                },
                ToutiaoChallengeClient.USER_STATUS_PATH: {
                    "code": 0,
                    "data": {"user_status": {"status": 1, "award": "10元"}},
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ToutiaoChallengeClient({}, Path(directory), protocol=protocol)
            detail = client.detail("7788")

        self.assertEqual(detail["id"], "7788")
        self.assertTrue(detail["participated"])
        self.assertEqual({item["type"] for item in detail["publish_types"]}, {"article", "video"})
        self.assertTrue(all(item["activity_tag"] == "7788" for item in detail["publish_types"]))
        self.assertIn("围绕车型体验创作", detail["blocks"][0]["text"])
        self.assertIn("主题内容不少于全文 50%", detail["generation_guidance"])
        self.assertNotIn("声明正文", detail["generation_guidance"])

    def test_html_to_text_preserves_block_boundaries(self) -> None:
        self.assertEqual(html_to_text("<p>第一段</p><p>第二段<br>下一行</p>"), "第一段\n第二段\n下一行")


if __name__ == "__main__":
    unittest.main()
