from __future__ import annotations

import unittest
from unittest.mock import patch

from hot_topics import HotTopicService


def row(source: str, source_name: str, rank: int, title: str, hint: str = "") -> dict[str, object]:
    return {
        "source_key": source,
        "source": source_name,
        "source_rank": rank,
        "title": title,
        "url": f"https://{source}.test/{rank}",
        "hot_value": 1000 - rank,
        "label": "实时",
        "category_hint": hint,
    }


class HotTopicServiceTests(unittest.TestCase):
    def test_multi_source_deduplication_ranking_and_classification(self) -> None:
        service = HotTopicService(
            {
                "hot_topics": {
                    "sources": ["toutiao", "baidu", "csdn"],
                    "per_source_limit": 10,
                    "limit": 30,
                }
            }
        )
        fixtures = {
            "toutiao": [
                row("toutiao", "头条热榜", 1, "人工智能芯片发布"),
                row("toutiao", "头条热榜", 2, "暴雨救援最新进展"),
            ],
            "baidu": [row("baidu", "百度热搜", 1, "人工智能芯片发布")],
            "csdn": [row("csdn", "CSDN 热榜", 1, "数据库性能优化", "编程 软件")],
        }

        with patch.object(
            service,
            "_fetch_source",
            side_effect=lambda source: (fixtures[source], 12),
        ):
            topics = service.fetch(force=True)

        self.assertEqual(topics[0]["title"], "人工智能芯片发布")
        self.assertEqual(topics[0]["source_count"], 2)
        self.assertEqual(topics[0]["category"], "科技")
        self.assertEqual(
            next(item for item in topics if item["title"] == "暴雨救援最新进展")["category"],
            "社会",
        )
        snapshot = service.snapshot()
        self.assertEqual(snapshot["healthy_sources"], 3)
        self.assertEqual(snapshot["total"], 3)
        self.assertTrue(any(item["name"] == "科技" for item in snapshot["categories"]))

    def test_failed_source_uses_its_previous_cache(self) -> None:
        service = HotTopicService(
            {"hot_topics": {"sources": ["toutiao", "baidu"], "cache_seconds": 15}}
        )
        fixtures = {
            "toutiao": [row("toutiao", "头条热榜", 1, "头条热点")],
            "baidu": [row("baidu", "百度热搜", 1, "百度热点")],
        }
        with patch.object(
            service,
            "_fetch_source",
            side_effect=lambda source: (fixtures[source], 8),
        ):
            service.fetch(force=True)

        def partly_failed(source: str) -> tuple[list[dict[str, object]], int]:
            if source == "baidu":
                raise TimeoutError("fixture timeout")
            return fixtures[source], 9

        with patch.object(service, "_fetch_source", side_effect=partly_failed):
            topics = service.fetch(force=True)

        self.assertEqual({item["title"] for item in topics}, {"头条热点", "百度热点"})
        baidu = next(item for item in service.snapshot()["sources"] if item["id"] == "baidu")
        self.assertEqual(baidu["status"], "stale")


if __name__ == "__main__":
    unittest.main()
