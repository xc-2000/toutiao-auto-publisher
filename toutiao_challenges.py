"""Toutiao creator activity discovery over the creator-platform protocol."""

from __future__ import annotations

import copy
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from PIL import Image

from toutiao_protocol import PublisherError, ToutiaoProtocolClient


LOG = logging.getLogger("toutiao-challenges")


def activity_reward_value(activity: Mapping[str, Any]) -> int:
    """Return the platform reward estimate used to rank monetization opportunities."""
    try:
        return max(0, int(activity.get("max_award") or 0))
    except (TypeError, ValueError):
        return 0


def _activity_terms(value: Any) -> set[str]:
    terms: set[str] = set()
    for run in re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", str(value or "").lower()):
        terms.add(run)
        if len(run) >= 4 and all("\u4e00" <= char <= "\u9fff" for char in run):
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def score_activity_for_topic(
    topic: Mapping[str, Any], activity: Mapping[str, Any]
) -> float:
    """Rank an activity by topic relevance first, then by its advertised reward."""
    topic_title = str(topic.get("title") or "")
    activity_title = str(activity.get("title") or "")
    activity_text = " ".join(
        str(activity.get(key) or "")
        for key in ("title", "introduction", "category", "activity_reward", "reward_label")
    ).lower()
    score = min(20.0, activity_reward_value(activity) / 5000.0)
    if str(topic.get("category") or "").strip() and str(topic.get("category") or "").strip() == str(activity.get("category") or "").strip():
        score += 12.0
    if topic_title and (topic_title.lower() in activity_text or activity_title.lower() in topic_title.lower()):
        score += 24.0
    overlap = _activity_terms(topic_title) & _activity_terms(activity_text)
    score += min(30.0, len(overlap) * 4.0)
    return round(score, 3)


class _ActivityTextParser(HTMLParser):
    BLOCK_TAGS = {"br", "div", "h1", "h2", "h3", "li", "p"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        lines = [line.strip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


class _MagicMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.image = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(name): str(value or "") for name, value in attrs}
        if tag == "title":
            self._in_title = True
        if tag != "meta":
            return
        key = attributes.get("name") or attributes.get("property")
        content = attributes.get("content", "").strip()
        if key in {"description", "og:description"} and content and not self.description:
            self.description = content
        elif key in {"og:image", "aweme:image"} and content and not self.image:
            self.image = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()


def html_to_text(value: Any) -> str:
    parser = _ActivityTextParser()
    parser.feed(str(value or ""))
    parser.close()
    return parser.text()


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso_time(value: Any) -> str:
    timestamp = _integer(value)
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


_DAILY_REPEAT_PATTERNS = (
    r"(?:每人)?每天(?:可|均可|都可|都能|最多|限|仅限|能够|能|有|获得|领取|完成|参与|投稿|发布|打卡|签到)",
    r"(?:每人)?每日(?:可|均可|都可|都能|最多|限|仅限|任务|奖励|抽签|打卡|签到|领取|完成|参与|投稿|发布|更新|瓜分|赢|得)",
    r"天天(?:可|都可|参与|投稿|发布|赢|领|分|奖励)",
    r"(?:按日|逐日|日更)",
)
_ONE_TIME_PATTERNS = (
    r"(?:每人|单个账号).{0,8}(?:仅限|限)(?:参与|投稿|领取|报名)?\s*[一1]次",
    r"(?:仅可|只能|仅限).{0,6}(?:参与|投稿|领取|报名)\s*[一1]次",
    r"(?:一次性|首次参与)",
)
_WEEKLY_REPEAT_PATTERNS = (
    r"(?:每人)?每周(?:可|均可|都可|最多|限|仅限|参与|领取|完成|投稿|发布|更新|任务)",
    r"(?:按周|逐周|周任务|每星期)",
)


def detect_repeat_mode(*values: Any) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", " ".join(str(value or "") for value in values)).strip()
    for pattern in _DAILY_REPEAT_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return "daily", f"规则命中“{match.group(0)}”"
    for pattern in _WEEKLY_REPEAT_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return "weekly", f"规则命中“{match.group(0)}”"
    for pattern in _ONE_TIME_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return "once", f"规则命中“{match.group(0)}”"
    return "unknown", "未发现每日重复规则"


def _embedded_json_values(source: str, name: str) -> list[Any]:
    pattern = re.compile(rf'["\']{re.escape(name)}["\']\s*:\s*')
    decoder = json.JSONDecoder()
    candidates: list[Any] = []
    for match in pattern.finditer(source):
        try:
            value, _ = decoder.raw_decode(source, match.end())
            candidates.append(value)
        except json.JSONDecodeError:
            continue
    return candidates


def _embedded_json_property(source: str, name: str) -> Any:
    candidates = _embedded_json_values(source, name)
    if not candidates:
        return None
    list_candidates = [value for value in candidates if isinstance(value, list)]
    return max(list_candidates, key=len) if list_candidates else candidates[0]


def _cash_label(value: Any) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return str(value or "").strip()
    yuan = amount / 100
    return f"{yuan:g} 元"


def _content_type_label(value: Any) -> str:
    labels = {
        "graph": "图文",
        "tuwen": "图文",
        "thread": "微头条",
        "weitoutiao": "微头条",
        "video": "视频",
        "microvideo": "小视频",
    }
    items = [labels.get(item.strip(), item.strip()) for item in str(value or "").split(",")]
    return "/".join(dict.fromkeys(item for item in items if item))


def clean_ocr_rule_text(value: str) -> str:
    keywords = (
        "活动",
        "任务",
        "用户",
        "回复",
        "点赞",
        "评论",
        "互动",
        "参与",
        "每周",
        "每日",
        "每天",
        "累计",
        "瓜分",
        "奖励",
        "发放",
        "时间",
        "现金",
    )
    lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" |<>-_")
        if len(line) < 4 or not any(keyword in line for keyword in keywords):
            continue
        if line not in lines:
            lines.append(line)
    return "\n".join(lines) if len(lines) >= 3 else str(value or "").strip()


_MAGIC_MEDIA_HOST_SUFFIXES = (
    "byteimg.com",
    "toutiaostatic.com",
    "douyinstatic.com",
    "huoshanstatic.com",
)


def magic_rule_image_urls(html: str) -> list[str]:
    values = _embedded_json_values(html, "imgSrc")
    if not values:
        for property_name in ("imageUrl", "image_url", "src", "backgroundImage"):
            values.extend(_embedded_json_values(html, property_name))
    urls: list[str] = []
    for value in values:
        image_url = str(value or "").strip().strip("'\"")
        match = re.fullmatch(r"url\(['\"]?(.*?)['\"]?\)", image_url)
        if match:
            image_url = match.group(1)
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        parsed_url = urlparse(image_url)
        if (
            parsed_url.scheme == "https"
            and parsed_url.hostname
            and parsed_url.hostname.endswith(_MAGIC_MEDIA_HOST_SUFFIXES)
        ):
            urls.append(image_url)
    return list(dict.fromkeys(urls))[:8]


def parse_magic_activity_page(html: str, activity_id: str = "") -> dict[str, Any]:
    parser = _MagicMetaParser()
    parser.feed(html)
    parser.close()
    task_names = _embedded_json_property(html, "taskNameList")
    task_configs = _embedded_json_property(html, "taskConfigList")
    topics = _embedded_json_property(html, "topicList")
    if not isinstance(topics, list) or not topics:
        topics = _embedded_json_property(html, "forumIDMap")
    activity_types = _embedded_json_property(html, "activityType")
    if not isinstance(task_names, list):
        task_names = []
    if not isinstance(task_configs, list):
        task_configs = []
    if not isinstance(topics, list):
        topics = []
    if not isinstance(activity_types, list):
        activity_types = []

    matching_configs = [
        item
        for item in task_configs
        if isinstance(item, Mapping)
        and (not activity_id or str(item.get("activity_id") or "") == activity_id)
    ]
    if not matching_configs:
        matching_configs = [item for item in task_configs if isinstance(item, Mapping)]

    stages: list[Mapping[str, Any]] = []
    task_requirements: list[str] = []
    requirement_keys: set[tuple[str, int, str, str]] = set()
    activity_start = 0
    activity_end = 0
    for task in matching_configs:
        activity_start = min(
            [value for value in (activity_start, _integer(task.get("activity_start_time"))) if value > 0],
            default=0,
        )
        activity_end = max(activity_end, _integer(task.get("activity_end_time")))
        rule_data = task.get("rule_data")
        raw_stages = rule_data.get("rule") if isinstance(rule_data, Mapping) else []
        if not isinstance(raw_stages, list):
            continue
        stages.extend(item for item in raw_stages if isinstance(item, Mapping))
        for stage in raw_stages:
            if not isinstance(stage, Mapping):
                continue
            configs = stage.get("config")
            if not isinstance(configs, list):
                continue
            for config in configs:
                if not isinstance(config, Mapping):
                    continue
                name = str(
                    config.get("custom_task_name") or config.get("task_name") or "发布内容"
                ).strip()
                target = max(1, _integer(config.get("target_num"), 1))
                award = _cash_label(config.get("award_content"))
                content_type = _content_type_label(config.get("group_type"))
                key = (name, target, award, content_type)
                if key in requirement_keys:
                    continue
                requirement_keys.add(key)
                line = f"{name}：完成 {target} 篇"
                if content_type:
                    line += f"{content_type}内容"
                if award:
                    line += f"，现金奖励 {award}"
                task_requirements.append(line)

    task_labels = [
        str(item.get("label") or "").strip()
        for item in task_names
        if isinstance(item, Mapping) and str(item.get("label") or "").strip()
    ]
    stage_periods = [
        (_integer(stage.get("start_time")), _integer(stage.get("end_time")))
        for stage in stages
    ]
    daily_periods = [
        (start, end)
        for start, end in stage_periods
        if start > 0 and 0 < end - start <= 86_400
    ]
    daily = any(label.startswith(("每日", "每天", "天天")) for label in task_labels) or (
        len(daily_periods) >= 2 and len(daily_periods) == len(stage_periods)
    )
    weekly_periods = [
        (start, end)
        for start, end in stage_periods
        if start > 0 and 6 * 86_400 <= end - start <= 7 * 86_400
    ]
    weekly = not daily and (
        any(label.startswith(("每周", "每星期")) for label in task_labels)
        or (len(weekly_periods) >= 2 and len(weekly_periods) == len(stage_periods))
    )
    repeat_mode = "daily" if daily else "weekly" if weekly else "unknown"
    if daily_periods:
        repeat_reason = f"活动页配置包含 {len(daily_periods)} 个按日阶段"
    elif weekly_periods:
        repeat_reason = f"活动页配置包含 {len(weekly_periods)} 个按周阶段"
    else:
        repeat_reason = "未发现周期性重复规则"

    blocks: list[dict[str, str]] = []
    if parser.description:
        blocks.append({"title": "活动介绍", "text": parser.description})
    type_labels = [_content_type_label(item) for item in activity_types]
    type_labels = list(dict.fromkeys(item for item in type_labels if item))
    if type_labels:
        blocks.append({"title": "投稿类型", "text": " / ".join(type_labels)})
    if task_requirements:
        blocks.append({"title": "任务要求与奖励", "text": "\n".join(task_requirements[:20])})
    period_lines: list[str] = []
    if activity_start and activity_end:
        start_label = datetime.fromtimestamp(activity_start).strftime("%Y-%m-%d")
        end_label = datetime.fromtimestamp(activity_end).strftime("%Y-%m-%d")
        period_lines.append(f"活动时间：{start_label} 至 {end_label}")
    if daily_periods:
        period_lines.append(f"平台配置了 {len(daily_periods)} 个按日任务阶段，可每日参与")
    elif weekly_periods:
        period_lines.append(f"平台配置了 {len(weekly_periods)} 个按周任务阶段，可每周参与")
    if task_labels:
        period_lines.append(f"任务分组：{' / '.join(task_labels[:10])}")
    if period_lines:
        blocks.append({"title": "任务周期", "text": "\n".join(period_lines)})
    topic_names = [
        str(item.get("name") or "").strip()
        for item in topics
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    ]
    if topic_names:
        blocks.append({"title": "推荐话题", "text": "\n".join(topic_names[:20])})
    rule_images = magic_rule_image_urls(html)
    return {
        "title": parser.title,
        "description": parser.description,
        "banner": parser.image,
        "blocks": blocks,
        "repeat_mode": repeat_mode,
        "repeat_reason": repeat_reason,
        "daily_repeatable": repeat_mode == "daily",
        "weekly_repeatable": repeat_mode == "weekly",
        "rule_images": rule_images if not blocks else [],
        "rule_source": "toutiao-magic-page",
    }


class ToutiaoChallengeClient:
    LIST_PATH = "/mp/agw/activity/list/v2/"
    CATEGORY_PATH = "/mp/agw/activity/get_all_category/"
    DETAIL_PATH = "/mp/agw/activity/detail/v3/"
    USER_STATUS_PATH = "/mp/agw/activity/get_activity_article_api/"
    BIZ_PATH = "/mp/agw/activity/biz_id/"
    ACTIVE_STATUS = 2
    _MAGIC_CACHE: dict[str, dict[str, Any]] = {}
    _MAGIC_CACHE_LOCK = threading.RLock()

    def __init__(
        self,
        config: dict[str, Any],
        config_dir: Path,
        *,
        cookie_override: Mapping[str, str] | None = None,
        headers_override: Mapping[str, str] | None = None,
        protocol: ToutiaoProtocolClient | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.protocol = protocol or ToutiaoProtocolClient(
            config,
            config_dir,
            cookie_override=cookie_override,
            headers_override=headers_override,
        )
        self._owns_protocol = protocol is None
        self.protocol.session.headers["Referer"] = (
            f"{self.protocol.base_url}/profile_v4/activity/task-list"
        )

    def __enter__(self) -> "ToutiaoChallengeClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_protocol:
            self.protocol.__exit__(None, None, None)

    def suitable_biz(self) -> int:
        payload = self.protocol.request_json("GET", self.BIZ_PATH)
        return _integer(payload.get("biz_id"), 1) or 1

    def categories(self, biz_id: int = 1) -> list[str]:
        payload = self.protocol.request_json(
            "GET",
            self.CATEGORY_PATH,
            params={
                "act_status": self.ACTIVE_STATUS,
                "biz_id": 2 if biz_id == 2 else 1,
            },
        )
        data = payload.get("data")
        if not isinstance(data, list):
            return ["全部"]
        categories = [str(item).strip() for item in data if str(item).strip()]
        return categories or ["全部"]

    def _ocr_rule_images(self, image_urls: list[str]) -> str:
        executable = str(
            self.protocol.toutiao.get("tesseract_executable")
            or shutil.which("tesseract")
            or ""
        ).strip()
        tessdata_dir = Path(
            str(
                self.protocol.toutiao.get("tesseract_data_dir")
                or self.config_dir / "assets" / "tessdata"
            )
        )
        if not executable or not Path(executable).is_file():
            return ""
        if not (tessdata_dir / "chi_sim.traineddata").is_file():
            return ""
        texts: list[str] = []
        with tempfile.TemporaryDirectory(prefix="toutiao-rules-") as directory:
            for index, image_url in enumerate(image_urls[:5]):
                parsed = urlparse(image_url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or not parsed.hostname.endswith(_MAGIC_MEDIA_HOST_SUFFIXES)
                ):
                    continue
                try:
                    response = self.protocol.session.get(
                        image_url,
                        headers={"Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8"},
                        timeout=15,
                    )
                    content = bytes(response.content)
                    if int(response.status_code) >= 400 or not content or len(content) > 8_000_000:
                        continue
                    image_path = Path(directory) / f"rule-{index}.png"
                    with Image.open(io.BytesIO(content)) as image:
                        image.convert("RGB").save(image_path, "PNG")
                    completed = subprocess.run(
                        [
                            executable,
                            str(image_path),
                            "stdout",
                            "--tessdata-dir",
                            str(tessdata_dir),
                            "-l",
                            "chi_sim",
                            "--psm",
                            "6",
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=25,
                        check=False,
                    )
                    text = "\n".join(
                        line.strip()
                        for line in completed.stdout.splitlines()
                        if line.strip()
                    )
                    if text:
                        texts.append(text)
                except Exception as exc:
                    LOG.warning("Could not OCR activity rule image: %s", exc)
        return "\n\n".join(texts)[:20_000]

    def _magic_activity(self, activity_url: str, activity_id: str) -> dict[str, Any]:
        normalized_url = str(activity_url or "").strip()
        if normalized_url.startswith("//"):
            normalized_url = f"https:{normalized_url}"
        parsed = urlparse(normalized_url)
        allowed_hosts = {"api.toutiaoapi.com", "mp.toutiao.com"}
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            return {}
        cache_key = f"{activity_id}:{normalized_url}"
        with self._MAGIC_CACHE_LOCK:
            cached = self._MAGIC_CACHE.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached)
        try:
            response = self.protocol.session.get(
                normalized_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"{self.protocol.base_url}/profile_v4/activity/task-list",
                },
                timeout=15,
                allow_redirects=True,
            )
            if int(response.status_code) >= 400:
                return {}
            html = str(getattr(response, "text", ""))
            if not html or len(html) > 5_000_000:
                return {}
            detail = parse_magic_activity_page(html, activity_id)
            rule_images = detail.get("rule_images")
            if not detail.get("blocks") and isinstance(rule_images, list) and rule_images:
                ocr_text = self._ocr_rule_images([str(item) for item in rule_images])
                if ocr_text:
                    ocr_text = clean_ocr_rule_text(ocr_text)
                    detail["blocks"] = [
                        {"title": "活动规则（图片文字识别）", "text": ocr_text}
                    ]
                    repeat_mode, repeat_reason = detect_repeat_mode(ocr_text)
                    if repeat_mode in {"daily", "weekly", "once"}:
                        detail["repeat_mode"] = repeat_mode
                        detail["repeat_reason"] = repeat_reason
                        detail["daily_repeatable"] = repeat_mode == "daily"
                        detail["weekly_repeatable"] = repeat_mode == "weekly"
            with self._MAGIC_CACHE_LOCK:
                if len(self._MAGIC_CACHE) >= 512:
                    self._MAGIC_CACHE.pop(next(iter(self._MAGIC_CACHE)))
                self._MAGIC_CACHE[cache_key] = copy.deepcopy(detail)
            return detail
        except Exception as exc:
            LOG.warning("Could not read activity page %s: %s", activity_id, exc)
            return {}

    def enrich_activity(self, activity: dict[str, Any]) -> dict[str, Any]:
        if str(activity.get("repeat_mode") or "unknown") != "unknown":
            return activity
        detail_url = str(activity.get("detail_url") or "")
        magic = self._magic_activity(detail_url, str(activity.get("id") or ""))
        if magic.get("repeat_mode") in {"daily", "weekly", "once"}:
            activity["repeat_mode"] = magic["repeat_mode"]
            activity["repeat_reason"] = magic.get("repeat_reason", "")
            activity["daily_repeatable"] = magic["repeat_mode"] == "daily"
            activity["weekly_repeatable"] = magic["repeat_mode"] == "weekly"
            activity["rule_source"] = magic.get("rule_source", "")
        return activity

    def list(
        self,
        *,
        biz_id: int = 1,
        part_status: int = 0,
        category: str = "全部",
        query: str = "",
        page: int = 1,
        page_size: int = 10,
        include_categories: bool = True,
    ) -> dict[str, Any]:
        biz_id = 2 if biz_id == 2 else 1
        page = max(1, page)
        page_size = max(1, min(100, page_size))
        payload = self.protocol.request_json(
            "GET",
            self.LIST_PATH,
            params={
                "act_status": self.ACTIVE_STATUS,
                "part_status": part_status if part_status in {0, 1, 2} else 0,
                "category": category.strip() or "全部",
                "offset": (page - 1) * page_size,
                "limit": page_size,
                "title": query.strip(),
                "sort_type": 1,
                "biz_id": biz_id,
                "online_platform_index": 0,
                "media_id": 0,
                "enter_from": "",
                "enter_from_mp": 3 if biz_id == 2 else 2,
            },
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise PublisherError("创作活动列表响应缺少 data")
        rows = data.get("activity_list")
        activities = [
            self._normalize_activity(row, biz_id)
            for row in rows
            if isinstance(row, Mapping)
        ] if isinstance(rows, list) else []
        return {
            "activities": activities,
            "total": _integer(data.get("total_num")),
            "page": page,
            "page_size": page_size,
            "biz_id": biz_id,
            "categories": self.categories(biz_id) if include_categories else [],
            "protocol": "toutiao-activity-http",
        }

    def list_all(
        self,
        *,
        biz_id: int = 1,
        part_status: int = 0,
        category: str = "全部",
        query: str = "",
    ) -> list[dict[str, Any]]:
        """Read every currently active activity for an account and media type."""
        page_size = 100
        first = self.list(
            biz_id=biz_id,
            part_status=part_status,
            category=category,
            query=query,
            page=1,
            page_size=page_size,
            include_categories=False,
        )
        activities = [
            item for item in first.get("activities", []) if isinstance(item, dict)
        ]
        total = max(len(activities), _integer(first.get("total")))
        total_pages = min(100, max(1, (total + page_size - 1) // page_size))
        for page in range(2, total_pages + 1):
            result = self.list(
                biz_id=biz_id,
                part_status=part_status,
                category=category,
                query=query,
                page=page,
                page_size=page_size,
                include_categories=False,
            )
            page_items = [
                item for item in result.get("activities", []) if isinstance(item, dict)
            ]
            if not page_items:
                break
            activities.extend(page_items)
        unique: dict[str, dict[str, Any]] = {}
        for activity in activities:
            activity_id = str(activity.get("id") or "").strip()
            if activity_id:
                unique[activity_id] = activity
        return list(unique.values())

    def detail(self, activity_id: str | int, *, activity_url: str = "") -> dict[str, Any]:
        normalized_id = str(activity_id).strip()
        if not normalized_id.isdigit():
            raise ValueError("活动 ID 格式无效")
        detail_payload = self.protocol.request_json(
            "GET", self.DETAIL_PATH, params={"activity_id": normalized_id}
        )
        status_payload = self.protocol.request_json(
            "GET", self.USER_STATUS_PATH, params={"activity_id": normalized_id}
        )
        data = detail_payload.get("data")
        if not isinstance(data, Mapping):
            raise PublisherError("创作活动详情响应缺少 data")
        blocks: list[dict[str, str]] = []
        raw_blocks = data.get("text_block")
        if isinstance(raw_blocks, list):
            for block in raw_blocks:
                if not isinstance(block, Mapping):
                    continue
                title = str(block.get("title") or "活动说明").strip()
                html = str(block.get("content") or "")
                blocks.append({"title": title, "text": html_to_text(html)})

        magic = self._magic_activity(activity_url, normalized_id) if not blocks else {}
        if not blocks and isinstance(magic.get("blocks"), list):
            blocks = [
                {"title": str(block.get("title") or "活动说明"), "text": str(block.get("text") or "")}
                for block in magic["blocks"]
                if isinstance(block, Mapping) and str(block.get("text") or "").strip()
            ]

        publish_types: list[dict[str, Any]] = []
        raw_types = data.get("activity_type")
        if isinstance(raw_types, Mapping):
            for platform_key, value in raw_types.items():
                if not isinstance(value, Mapping) or platform_key not in {"graph", "video"}:
                    continue
                publish_types.append(
                    {
                        "type": "article" if platform_key == "graph" else "video",
                        "label": str(value.get("label") or ("发表文章" if platform_key == "graph" else "发表视频")),
                        "forum_id": str(value.get("forum_id") or ""),
                        "activity_tag": str(value.get("id") or normalized_id),
                    }
                )

        user_data = status_payload.get("data")
        user_status = user_data.get("user_status") if isinstance(user_data, Mapping) else {}
        repeat_mode, repeat_reason = detect_repeat_mode(
            data.get("title"),
            *(block.get("text") for block in blocks),
        )
        if magic.get("repeat_mode") in {"daily", "weekly", "once"}:
            repeat_mode = str(magic["repeat_mode"])
            repeat_reason = str(magic.get("repeat_reason") or repeat_reason)
        detail = {
            "id": normalized_id,
            "title": str(data.get("title") or "创作活动"),
            "status": "active" if _integer(data.get("status")) < 3 else "ended",
            "status_code": _integer(data.get("status")),
            "banner": str(
                data.get("banner")
                or data.get("mobile_banner")
                or data.get("entry_picture")
                or magic.get("banner")
                or ""
            ),
            "blocks": blocks,
            "publish_types": publish_types,
            "participated": bool(
                isinstance(user_status, Mapping) and _integer(user_status.get("status")) > 0
            ),
            "award": str(user_status.get("award") or "") if isinstance(user_status, Mapping) else "",
            "repeat_mode": repeat_mode,
            "repeat_reason": repeat_reason,
            "daily_repeatable": repeat_mode == "daily",
            "weekly_repeatable": repeat_mode == "weekly",
            "rule_images": list(magic.get("rule_images") or []),
            "rule_source": str(magic.get("rule_source") or "toutiao-activity-detail"),
            "detail_url": str(activity_url or ""),
            "protocol": "toutiao-activity-http",
        }
        detail["generation_guidance"] = self.generation_guidance(detail)
        return detail

    @staticmethod
    def generation_guidance(detail: Mapping[str, Any], max_length: int = 6000) -> str:
        title = str(detail.get("title") or "创作活动")
        preferred = {"活动介绍", "内容要求", "参与方式", "评选规则"}
        blocks = detail.get("blocks") if isinstance(detail.get("blocks"), list) else []
        selected = [block for block in blocks if str(block.get("title")) in preferred]
        if not selected:
            selected = [block for block in blocks if str(block.get("title")) != "法律声明"]
        sections = [
            f"头条创作活动：{title}",
            "必须紧扣活动主题与投稿要求，正文自然展开，不得用无关内容硬转折蹭活动。",
        ]
        for block in selected:
            heading = str(block.get("title") or "活动要求").strip()
            content = str(block.get("text") or "").strip()
            if content:
                sections.append(f"【{heading}】\n{content}")
        return "\n\n".join(sections)[:max_length]

    @staticmethod
    def _normalize_activity(row: Mapping[str, Any], requested_biz_id: int) -> dict[str, Any]:
        status_code = _integer(row.get("status"))
        activity_id = str(row.get("activity_id") or "")
        repeat_mode, repeat_reason = detect_repeat_mode(
            row.get("title"),
            row.get("introduction"),
            row.get("agg_page_introduction"),
        )
        detail_url = str(
            row.get("href")
            or row.get("echo_url")
            or row.get("toutiao_mobile_href")
            or row.get("xigua_mobile_href")
            or row.get("xigua_pc_href")
            or ""
        )
        return {
            "id": activity_id,
            "title": str(row.get("title") or "创作活动"),
            "introduction": str(row.get("introduction") or ""),
            "content_type": "video" if requested_biz_id == 2 else "article",
            "biz_id": requested_biz_id,
            "status": "active" if status_code < 3 else "ended",
            "status_code": status_code,
            "participated": bool(_integer(row.get("part_in"))),
            "participants": _integer(row.get("part_num")),
            "participants_label": str(row.get("activity_participants") or ""),
            "max_award": _integer(row.get("max_award")),
            "reward_label": str(row.get("activity_reward") or ""),
            "activity_time": str(row.get("activity_time") or ""),
            "starts_at": _iso_time(row.get("activity_start_time")),
            "ends_at": _iso_time(row.get("activity_end_time")),
            "category": str(row.get("category") or ""),
            "creator": str(row.get("user_name") or row.get("activity_creator") or "头条官方"),
            "forum_id": str(row.get("forum_id") or ""),
            "fresh": bool(_integer(row.get("fresh"))),
            "repeat_mode": repeat_mode,
            "repeat_reason": repeat_reason,
            "daily_repeatable": repeat_mode == "daily",
            "weekly_repeatable": repeat_mode == "weekly",
            "detail_url": detail_url,
        }
