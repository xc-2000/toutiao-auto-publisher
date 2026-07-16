import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from toutiao_publisher import ArticleGenerator, find_editorial_meta


def response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))]
    )


def article_payload(body: str) -> dict:
    return {
        "title": "法国输球后十五秒视频引发关注",
        "summary": "一段现场视频快速传播，事件本身与传播背景都值得核实。",
        "body_markdown": body,
        "tags": ["法国", "短视频", "热点"],
    }


class ArticleGenerationTests(unittest.TestCase):
    def test_prompt_requests_publishable_copy_without_editorial_process(self) -> None:
        generator = object.__new__(ArticleGenerator)
        generator.content = {
            "keywords": ["热点"],
            "account_positioning": "热点解读",
            "audience": "普通读者",
            "tone": "客观",
            "word_count": 1000,
            "call_to_action": "欢迎讨论",
        }

        prompt = generator._prompt("测试选题", "从事实和影响两个层面展开")

        self.assertIn("只交付思考后的成稿", prompt)
        self.assertIn("不得展示写作思路", prompt)
        self.assertIn("不是策划案、提纲或创作教程", prompt)

    def test_editorial_meta_detector_matches_outline_language(self) -> None:
        body = "## 15秒视频为何容易爆火\n这些内容的爆点通常来自三点：冲突、悬念和细节。"

        self.assertEqual(find_editorial_meta(body), ["爆点通常", "内容的爆点"])
        self.assertEqual(find_editorial_meta("## 事件经过\n现场视频随后在社交平台传播。"), [])

    def test_generator_rewrites_editorial_meta_content_once(self) -> None:
        bad_body = (
            "## 传播分析\n这些内容的爆点通常来自冲突和悬念。"
            + "这是写作思路而不是最终正文。" * 30
        )
        final_body = (
            "## 事件经过\n比赛结束后，现场拍摄的视频开始在社交平台传播。"
            "公开信息显示，讨论主要集中在比赛结果和现场反应。"
            "## 信息需要交叉核实\n短视频只能呈现有限片段，判断完整经过仍需结合赛事记录、"
            "当事方公开回应和多个独立来源。"
            "## 如何看待这次传播\n面对快速升温的话题，先区分可验证事实与网友推测，"
            "再讨论事件影响，能减少片段信息造成的误判。" * 5
        )
        client = Mock()
        client.chat.completions.create.side_effect = [
            response(article_payload(bad_body)),
            response(article_payload(final_body)),
        ]
        generator = object.__new__(ArticleGenerator)
        generator.client = client
        generator.model = "test-model"
        generator.temperature = 0.3
        generator.json_mode = True
        generator.content = {
            "keywords": ["热点"],
            "account_positioning": "热点解读",
            "audience": "普通读者",
            "tone": "客观",
            "word_count": 1000,
            "call_to_action": "欢迎讨论",
        }

        article = generator.generate("法国输球后相关视频引发关注", "核实事实后解释影响")

        self.assertEqual(article.body_markdown, final_body)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        repair_messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertIn("删除这些元内容", repair_messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
