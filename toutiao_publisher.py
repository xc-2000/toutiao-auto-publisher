#!/usr/bin/env python3
"""Generate original articles and publish them through Toutiao Creator Center."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import sys
import time
import tomllib
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

try:
    from net_utils import prefer_ipv4

    prefer_ipv4()
except Exception:
    pass
from PIL import Image, ImageDraw, ImageFont, ImageOps

from toutiao_protocol import PublisherError, ToutiaoProtocolClient


LOG = logging.getLogger("toutiao-publisher")


EDITORIAL_META_MARKERS = (
    "写作思路",
    "创作思路",
    "选题思路",
    "内容思路",
    "成文思路",
    "写作角度",
    "爆点分析",
    "爆点通常",
    "内容的爆点",
    "为什么值得写",
    "可写方向",
    "创作方向",
    "写作建议",
    "创作建议",
    "选题建议",
    "素材建议",
    "文章结构建议",
    "内容结构建议",
)


def find_editorial_meta(body_markdown: str) -> list[str]:
    """Return editorial-planning phrases that must stay out of publishable copy."""
    compact = re.sub(r"\s+", "", str(body_markdown or ""))
    return [marker for marker in EDITORIAL_META_MARKERS if marker in compact]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slug(value: str, limit: int = 40) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE)
    return value.strip("-")[:limit] or "article"


def load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8-sig"))


def resolve_path(config_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_dir / path).resolve()


def extract_json(text: str) -> dict[str, Any]:
    """Parse model JSON even when trailing prose / multi-json / fences exist."""
    raw = (text or "").strip()
    if not raw:
        raise PublisherError("AI response is empty")

    # strip markdown fences (full or partial)
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    else:
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()

    decoder = json.JSONDecoder()
    candidates: list[str] = [raw]

    # if model prepended explanation, start from first object/array
    for opener in ("{", "["):
        idx = raw.find(opener)
        if idx > 0:
            candidates.append(raw[idx:])

    # common: JSON then Chinese explanation / second JSON
    for candidate in list(candidates):
        # also try removing trailing commas before } ]
        cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
        if cleaned != candidate:
            candidates.append(cleaned)

    last_error: Exception | None = None
    for candidate in candidates:
        data = candidate.lstrip()
        if not data:
            continue
        try:
            payload, _end = decoder.raw_decode(data)
        except json.JSONDecodeError as exc:
            last_error = exc
            # brace-scan first complete object
            start = data.find("{")
            if start < 0:
                continue
            depth = 0
            in_str = False
            escape = False
            end = -1
            for i, ch in enumerate(data[start:], start=start):
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end > start:
                snippet = data[start : end + 1]
                try:
                    payload = json.loads(snippet)
                except json.JSONDecodeError as exc2:
                    last_error = exc2
                    continue
            else:
                continue
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            payload = payload[0]
        if isinstance(payload, dict):
            return payload
        last_error = PublisherError("AI response JSON must be an object")

    detail = str(last_error) if last_error else "unknown"
    preview = raw[:240].replace("\n", " ")
    raise PublisherError(f"AI response JSON parse failed: {detail}; preview={preview!r}")


def markdown_to_html(markdown: str) -> str:
    """Render a deliberately small Markdown subset into editor-safe HTML."""
    output: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(part.strip() for part in paragraph if part.strip())
            output.append(f"<p>{html.escape(text)}</p>")
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{html.escape(item)}</li>" for item in bullets)
            output.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw_line in markdown.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_bullets()
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_bullets()
            output.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif re.match(r"^[-*]\s+", line):
            flush_paragraph()
            bullets.append(re.sub(r"^[-*]\s+", "", line))
        else:
            flush_bullets()
            paragraph.append(line)

    flush_paragraph()
    flush_bullets()
    return "\n".join(output)


@dataclass(slots=True)
class Article:
    topic: str
    title: str
    summary: str
    body_markdown: str
    tags: list[str]
    generated_at: str

    @classmethod
    def from_payload(cls, topic: str, payload: dict[str, Any]) -> "Article":
        title = str(payload.get("title", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        body = str(payload.get("body_markdown", "")).strip()
        tags_value = payload.get("tags") or []
        tags = [str(item).strip() for item in tags_value if str(item).strip()][:5]
        if not 5 <= len(title) <= 30:
            raise PublisherError(f"Generated title length must be 5-30 characters: {title!r}")
        if len(body) < 300:
            raise PublisherError("Generated body is too short")
        return cls(
            topic=topic,
            title=title,
            summary=summary,
            body_markdown=body,
            tags=tags,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @property
    def body_html(self) -> str:
        return markdown_to_html(self.body_markdown)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["body_html"] = self.body_html
        return payload

    @classmethod
    def load(cls, path: Path) -> "Article":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("body_html", None)
        return cls(**payload)




def _is_transient_api_error(exc: BaseException) -> bool:
    """Detect temporary upstream API failures that are worth retrying."""
    text = str(exc) or ""
    low = text.lower()
    markers = (
        "503",
        "502",
        "504",
        "429",
        "500",
        "service temporarily unavailable",
        "temporarily unavailable",
        "rate limit",
        "overloaded",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "server error",
        "bad gateway",
        "gateway timeout",
        "api_error",
    )
    if any(m in low for m in markers):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) in {408, 409, 425, 429, 500, 502, 503, 504}
    except (TypeError, ValueError):
        return False


def _call_with_retries(func, *, what: str, attempts: int = 5, base_delay: float = 2.0):
    last: BaseException | None = None
    for i in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i >= attempts or not _is_transient_api_error(exc):
                raise
            delay = min(30.0, base_delay * (2 ** (i - 1)))
            LOG.warning("%s transient failure (%s/%s): %s; retry in %.1fs", what, i, attempts, exc, delay)
            time.sleep(delay)
    assert last is not None
    raise last

class ArticleGenerator:
    def __init__(self, config: dict[str, Any]) -> None:
        ai = config["ai"]
        key = str(ai.get("api_key") or os.getenv(str(ai.get("api_key_env", "OPENAI_API_KEY")), "")).strip()
        if not key:
            raise PublisherError(
                f"Missing API key environment variable: {ai.get('api_key_env', 'OPENAI_API_KEY')}"
            )
        self.client = OpenAI(api_key=key, base_url=str(ai.get("base_url") or "").rstrip("/") or None, max_retries=2, timeout=120.0)
        self.model = str(ai["model"])
        self.temperature = float(ai.get("temperature", 0.7))
        self.json_mode = bool(ai.get("json_mode", True))
        self.content = config["content"]

    def generate(self, topic: str, guidance: str = "") -> Article:
        prompt = self._prompt(topic, guidance)
        system_message = (
            "你是中文头条号资深编辑。输出原创、事实谨慎、结构清晰的文章。"
            "不要虚构数据、采访、政策或来源；不确定的事实用审慎表述。"
            "你只交付面向读者的最终成稿。写作计划、分析过程、爆点拆解、"
            "选题依据、内容结构和创作建议都属于内部工作，不得写进标题、摘要或正文。"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        LOG.info("Generating article for topic: %s", topic)

        def _create(use_json: bool = True):
            call_kwargs = dict(kwargs)
            if not use_json:
                call_kwargs.pop("response_format", None)
            return self.client.chat.completions.create(**call_kwargs)

        for content_attempt in range(2):
            kwargs["messages"] = messages
            try:
                response = _call_with_retries(
                    lambda: _create(True), what=f"article:{topic[:40]}"
                )
            except Exception as exc:
                if self.json_mode and "response_format" in str(exc):
                    LOG.warning("Provider rejected JSON mode; retrying without response_format")
                    kwargs.pop("response_format", None)
                    response = _call_with_retries(
                        lambda: _create(False), what=f"article-nojson:{topic[:40]}"
                    )
                else:
                    raise

            text = response.choices[0].message.content or ""
            article = Article.from_payload(topic, extract_json(text))
            meta_markers = find_editorial_meta(article.body_markdown)
            if not meta_markers:
                return article
            if content_attempt == 0:
                LOG.warning(
                    "Generated body contains editorial meta content (%s); rewriting once",
                    ", ".join(meta_markers),
                )
                messages = [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            "上一版把编辑策划内容写进了正文。请重新输出完整 JSON 成稿，"
                            f"删除这些元内容：{'、'.join(meta_markers)}。"
                            "正文只保留直接面向读者的事实、叙事、分析、结论和可执行信息；"
                            "不要解释为什么这样写，也不要展示构思、提纲或创作方法。"
                        ),
                    },
                ]
                continue
            raise PublisherError(
                "Generated body still contains editorial planning content: "
                + ", ".join(meta_markers)
            )
        raise PublisherError("Article generation did not produce publishable copy")

    def _prompt(self, topic: str, guidance: str = "") -> str:
        keywords = "、".join(str(x) for x in self.content.get("keywords", []))
        guidance_line = guidance.strip()
        if not guidance_line:
            try:
                from hot_topics import suggest_writing_angle

                guidance_line = suggest_writing_angle(topic)
            except Exception:
                guidance_line = "围绕选题核心信息，先给结论，再写背景、影响与可执行建议"
        return f"""
围绕以下选题写一篇头条号文章：{topic}

账号定位：{self.content.get('account_positioning', '')}
目标读者：{self.content.get('audience', '')}
语气：{self.content.get('tone', '')}
目标长度：约 {int(self.content.get('word_count', 1200))} 个中文字
关键词：{keywords}
结尾要求：{self.content.get('call_to_action', '')}
写作角度（请严格按此角度组织，不要套用固定模板）：{guidance_line}

“写作角度”仅供内部构思。先完成事实梳理、判断和结构设计，再只交付思考后的成稿；
不得在正文中复述或解释写作角度，不得展示写作思路、选题思路、爆点分析、内容提纲、
素材建议、创作建议或“为什么值得写”等编辑过程。

写作要求：
1. 标题 5-30 个字符，具体、准确，不使用震惊体和虚假承诺。
2. 开头直接呈现读者问题或核心结论，并贴合上述写作角度。
3. 正文包含 3-6 个二级标题，用“## 标题”表示；二级标题要服务该角度，避免千篇一律的“背景/影响/建议”三板斧除非角度需要。
4. 给出可执行步骤、适用条件和常见误区。
5. 内容原创，避免复述已有文章，不编造数据和案例。
6. `body_markdown` 必须是读者可直接阅读和发布的完整文章，不是策划案、提纲或创作教程。
7. 只输出下列 JSON，不添加说明、分析过程或代码围栏：
{{
  "title": "文章标题",
  "summary": "80 字以内摘要",
  "body_markdown": "完整正文，使用 ## 二级标题和 - 列表",
  "tags": ["标签1", "标签2", "标签3"]
}}
""".strip()



class CoverGenerator:
    """Generate diversified 3:2 covers based on article content theme."""

    OUTPUT_SIZE = (1200, 800)

    THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
        "科技": ("ai", "人工智能", "大模型", "芯片", "手机", "软件", "互联网", "算法", "数码", "智能", "机器人", "科技", "编程", "数据", "云", "5g", "元宇宙"),
        "财经": ("股市", "股票", "基金", "银行", "金融", "经济", "房价", "楼市", "投资", "上市", "财报", "消费", "降息", "涨价", "市值", "人民币", "美元"),
        "社会": ("警方", "法院", "事故", "暴雨", "台风", "地震", "火灾", "救援", "通报", "辟谣", "应急", "民生", "社区", "城市", "公共"),
        "健康": ("健康", "医生", "医院", "疾病", "药品", "疫苗", "感染", "减肥", "睡眠", "营养", "心理", "养生", "癌症", "急救"),
        "教育": ("大学", "高校", "学校", "学生", "老师", "高考", "中考", "考研", "教育", "课程", "留学", "毕业", "校园"),
        "娱乐": ("电影", "电视剧", "综艺", "明星", "演员", "歌手", "演唱会", "娱乐", "票房", "短剧", "直播"),
        "体育": ("足球", "篮球", "nba", "世界杯", "奥运", "比赛", "冠军", "运动员", "中超", "网球", "赛事"),
        "汽车": ("汽车", "车企", "新能源", "电动车", "智驾", "特斯拉", "比亚迪", "车主", "车型", "续航", "充电"),
        "国际": ("美国", "俄罗斯", "日本", "韩国", "欧洲", "欧盟", "联合国", "国际", "外交", "总统", "战争"),
        "生活": ("旅行", "旅游", "美食", "餐厅", "家居", "宠物", "职场", "家庭", "育儿", "购物", "生活", "咖啡", "穿搭"),
    }

    THEME_VISUALS: dict[str, dict[str, object]] = {
        "科技": {
            "subjects": (
                "futuristic lab workstation with dual monitors and holographic UI reflections",
                "close-up of smartphone and circuit board under cool neon ambient light",
                "modern data center corridor with soft blue rack lights",
                "designer desk with laptop, stylus tablet and abstract AI visualization",
            ),
            "style": "clean tech editorial, cool cyan-blue palette, shallow depth of field",
            "brand": "科技前沿",
            "accent": (34, 211, 238),
            "layouts": ("bottom_bar", "left_panel", "gradient_caption"),
        },
        "财经": {
            "subjects": (
                "trading desk with market charts softly blurred in background",
                "modern glass office skyline at dusk suggesting finance district",
                "hands reviewing printed reports beside laptop and coffee",
                "macro shot of currency notes and calculator on dark wood desk",
            ),
            "style": "business magazine photography, deep navy and gold tones",
            "brand": "财经观察",
            "accent": (234, 179, 8),
            "layouts": ("bottom_bar", "top_banner", "corner_card"),
        },
        "社会": {
            "subjects": (
                "city street documentary scene after rain with reflective pavement",
                "community volunteers assisting residents in a realistic urban setting",
                "newsroom assignment board and camera bag ready for field reporting",
                "wide urban intersection at blue hour with authentic crowd motion blur",
            ),
            "style": "documentary photojournalism, naturalistic color, candid framing",
            "brand": "社会现场",
            "accent": (248, 113, 113),
            "layouts": ("bottom_bar", "gradient_caption", "left_panel"),
        },
        "健康": {
            "subjects": (
                "bright clinic consultation desk with stethoscope and notes",
                "morning outdoor jogging path with soft sunlight and greenery",
                "balanced healthy meal prep on clean kitchen counter",
                "pharmacist shelf with organized medicine bottles in soft focus",
            ),
            "style": "fresh healthcare editorial, soft whites and mint greens",
            "brand": "健康参考",
            "accent": (52, 211, 153),
            "layouts": ("bottom_bar", "corner_card", "minimal_caption"),
        },
        "教育": {
            "subjects": (
                "university library aisle with warm reading lamps",
                "student notebook, highlighter and tablet on study desk",
                "classroom whiteboard with abstract diagrams out of focus",
                "graduation cap and books arranged as still-life editorial",
            ),
            "style": "academic lifestyle photography, warm paper tones",
            "brand": "教育观察",
            "accent": (96, 165, 250),
            "layouts": ("left_panel", "bottom_bar", "top_banner"),
        },
        "娱乐": {
            "subjects": (
                "cinema seats with glowing screen bokeh in background",
                "concert stage lights with dramatic colored haze",
                "creative studio set with softboxes and wardrobe rack",
                "night city entertainment district neon reflections",
            ),
            "style": "entertainment magazine, vivid contrast, cinematic lighting",
            "brand": "文娱热议",
            "accent": (244, 114, 182),
            "layouts": ("gradient_caption", "corner_card", "bottom_bar"),
        },
        "体育": {
            "subjects": (
                "athlete silhouette sprinting on track under stadium lights",
                "close-up of football boots and turf texture",
                "basketball court lines with dramatic side light",
                "sports watch and water bottle on gym bench",
            ),
            "style": "dynamic sports editorial, high contrast motion energy",
            "brand": "体育速览",
            "accent": (251, 146, 60),
            "layouts": ("bottom_bar", "left_panel", "gradient_caption"),
        },
        "汽车": {
            "subjects": (
                "modern EV charging at contemporary glass building",
                "driver-seat perspective of dashboard and city road ahead",
                "sleek car exterior detail with rain droplets and reflections",
                "showroom floor with single car under soft spotlight",
            ),
            "style": "automotive advertising editorial, metallic reflections",
            "brand": "车市观察",
            "accent": (56, 189, 248),
            "layouts": ("bottom_bar", "minimal_caption", "corner_card"),
        },
        "国际": {
            "subjects": (
                "world map wall in modern briefing room with soft daylight",
                "international airport terminal walkway with travel atmosphere",
                "embassy-like classical architecture facade at golden hour",
                "newsroom globe and multi-language newspaper stack still life",
            ),
            "style": "global affairs editorial, restrained palette, serious tone",
            "brand": "国际视线",
            "accent": (129, 140, 248),
            "layouts": ("top_banner", "bottom_bar", "left_panel"),
        },
        "生活": {
            "subjects": (
                "cozy cafe table with coffee, notebook and city window light",
                "home living room corner with plants and soft textiles",
                "weekend market produce baskets in natural daylight",
                "person packing light travel bag near apartment doorway",
            ),
            "style": "lifestyle editorial, warm natural light, human-scale details",
            "brand": "生活提案",
            "accent": (251, 191, 36),
            "layouts": ("corner_card", "minimal_caption", "bottom_bar"),
        },
        "综合": {
            "subjects": (
                "editorial still life of notebook, phone and newspaper on desk",
                "city balcony overlook at dusk with layered architecture",
                "hands typing on laptop with soft ambient desk lamp",
                "abstract geometric architecture facade with strong lines",
            ),
            "style": "general news magazine cover photography, balanced composition",
            "brand": "热点观察",
            "accent": (248, 113, 113),
            "layouts": ("bottom_bar", "gradient_caption", "left_panel", "corner_card", "top_banner", "minimal_caption"),
        },
    }

    def __init__(self, config: dict[str, Any], config_dir: Path) -> None:
        self.config = config
        self.config_dir = config_dir
        self.cover = config.get("cover", {})
        self.ai = config.get("ai", {})

    def generate(self, article: Article) -> Path:
        output_dir = resolve_path(self.config_dir, self.cover.get("output_dir", "./covers"))
        assert output_dir is not None
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{utc_stamp()}-{slug(article.title)}.jpg"

        theme = self._infer_theme(article)
        seed = self._content_seed(article, theme)
        image: Image.Image
        try:
            image = self._generate_image(article, theme, seed)
        except Exception:
            if not bool(self.cover.get("fallback_on_error", True)):
                raise
            LOG.exception("Image API failed; using diversified local cover generator")
            image = self._fallback_image(article, theme, seed)

        image = ImageOps.fit(image.convert("RGB"), self.OUTPUT_SIZE, method=Image.Resampling.LANCZOS)
        if bool(self.cover.get("overlay_title", True)):
            image = self._overlay(image, article, theme, seed)
        image.save(output, format="JPEG", quality=92, optimize=True)
        return output

    def _content_seed(self, article: Article, theme: str) -> int:
        blob = f"{article.title}|{article.summary}|{'/'.join(article.tags)}|{theme}"
        return int.from_bytes(hashlib.sha256(blob.encode("utf-8")).digest()[:8], "big")

    def _infer_theme(self, article: Article) -> str:
        text = f"{article.title} {article.summary} {' '.join(article.tags)}".lower()
        scores: dict[str, int] = {}
        for theme, words in self.THEME_KEYWORDS.items():
            score = 0
            for word in words:
                if word.lower() in text:
                    score += 2 if word in article.title else 1
            if score:
                scores[theme] = score
        if not scores:
            return "综合"
        # stable top theme, break ties by seed
        best = max(scores.values())
        tops = sorted([k for k, v in scores.items() if v == best])
        seed = self._content_seed(article, tops[0])
        return tops[seed % len(tops)]

    def _theme_profile(self, theme: str) -> dict[str, object]:
        return self.THEME_VISUALS.get(theme) or self.THEME_VISUALS["综合"]

    def _generate_image(self, article: Article, theme: str, seed: int) -> Image.Image:
        if not bool(self.cover.get("enabled", True)):
            return self._fallback_image(article, theme, seed)
        key_env = str(self.cover.get("api_key_env") or self.ai.get("api_key_env", "OPENAI_API_KEY"))
        key = str(self.cover.get("api_key") or os.getenv(key_env, "")).strip()
        if not key:
            raise PublisherError(f"Missing image API key environment variable: {key_env}")
        base_url = str(self.cover.get("base_url") or self.ai.get("base_url") or "").rstrip("/")
        client = OpenAI(api_key=key, base_url=base_url or None, max_retries=2, timeout=120.0)
        prompt = self._build_image_prompt(article, theme, seed)
        response = _call_with_retries(
            lambda: client.images.generate(
                model=str(self.cover.get("model", "gpt-image-1")),
                prompt=prompt,
                size=str(self.cover.get("size", "1536x1024")),
                quality=str(self.cover.get("quality", "medium")),
                n=1,
            ),
            what=f"cover:{article.title[:40]}",
        )
        item = response.data[0]
        if getattr(item, "b64_json", None):
            raw = base64.b64decode(item.b64_json)
        elif getattr(item, "url", None):
            request = urllib.request.Request(item.url, headers={"User-Agent": "toutiao-cover/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response_stream:
                raw = response_stream.read()
        else:
            raise PublisherError("Image API returned neither b64_json nor url")
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def _build_image_prompt(self, article: Article, theme: str, seed: int) -> str:
        profile = self._theme_profile(theme)
        subjects = list(profile.get("subjects") or ())
        subject = subjects[seed % len(subjects)] if subjects else "editorial still life relevant to the topic"
        style = str(profile.get("style") or "editorial photography")
        angles = (
            "slight low angle emphasizing the main subject",
            "eye-level documentary framing",
            "tight medium shot with strong foreground detail",
            "wide environmental context with clear hero subject",
            "over-the-shoulder perspective that still keeps subject readable",
        )
        moods = (
            "optimistic and practical",
            "calm and analytical",
            "urgent but not sensational",
            "warm human-centered",
            "cool professional",
        )
        compositions = (
            "leave clean darker negative space in the lower third for headline overlay",
            "keep left third simpler for vertical caption panel",
            "keep upper band less busy for a top headline strip",
            "reserve a soft lower-left corner area for caption card",
        )
        tags = "、".join(article.tags[:4]) if article.tags else "无"
        summary = (article.summary or "").strip()
        if len(summary) > 160:
            summary = summary[:160] + "…"
        return (
            "Create a unique high-quality Chinese media cover photo, 3:2 landscape. "
            f"Article theme category: {theme}. "
            f"Headline meaning: {article.title}. "
            f"Core context: {summary or article.title}. "
            f"Keywords: {tags}. "
            f"Primary visual: {subject}. "
            f"Visual style: {style}. "
            f"Camera: {angles[seed % len(angles)]}. "
            f"Mood: {moods[(seed // 3) % len(moods)]}. "
            f"Composition: {compositions[(seed // 5) % len(compositions)]}. "
            "Make the scene concretely match THIS article's subject matter, not a generic newsroom or laptop cliche unless the topic is about offices/tech work. "
            "Photorealistic, single coherent scene, natural materials, no collage, no poster design, "
            "no text, no letters, no numbers, no logos, no watermark, no UI mockups with readable text."
        )

    def _fallback_image(self, article: Article, theme: str | None = None, seed: int | None = None) -> Image.Image:
        theme = theme or self._infer_theme(article)
        seed = self._content_seed(article, theme) if seed is None else seed
        profile = self._theme_profile(theme)
        accent = tuple(profile.get("accent") or (221, 69, 55))  # type: ignore[arg-type]
        palettes = (
            ((18, 24, 32), accent, (230, 190, 90)),
            ((24, 28, 36), accent, (90, 150, 190)),
            ((30, 34, 42), accent, (120, 170, 130)),
            ((16, 22, 30), accent, (210, 120, 90)),
        )
        base, main, secondary = palettes[seed % len(palettes)]
        image = Image.new("RGB", self.OUTPUT_SIZE, base)
        draw = ImageDraw.Draw(image)
        layout = seed % 5
        if layout == 0:
            draw.rectangle((0, 0, 360, 800), fill=main)
            draw.rectangle((360, 0, 1200, 160), fill=secondary)
            for index in range(5):
                x = 420 + index * 140
                height = 120 + ((seed >> (index + 1)) & 0x7F)
                draw.rectangle((x, 720 - height, x + 70, 720), fill=(70, 80, 90))
        elif layout == 1:
            draw.ellipse((680, -120, 1380, 520), fill=main)
            draw.rectangle((0, 620, 1200, 800), fill=secondary)
            draw.rectangle((70, 90, 420, 430), fill=(45, 55, 65))
        elif layout == 2:
            draw.polygon([(0, 0), (1200, 0), (1200, 280), (0, 420)], fill=main)
            draw.rectangle((80, 480, 520, 720), fill=secondary)
            draw.rectangle((560, 520, 1120, 740), fill=(50, 58, 68))
        elif layout == 3:
            draw.rectangle((0, 0, 1200, 240), fill=main)
            for index in range(8):
                y = 280 + index * 55
                w = 300 + ((seed >> index) & 0xFF)
                draw.rectangle((80, y, 80 + w, y + 28), fill=secondary if index % 2 == 0 else (60, 70, 80))
        else:
            draw.rectangle((0, 500, 1200, 800), fill=main)
            draw.pieslice((-80, -80, 520, 520), 20, 250, fill=secondary)
            draw.rectangle((700, 120, 1120, 420), fill=(48, 56, 66))
        # subtle content hash noise bars for uniqueness
        for index in range(12):
            x = 40 + index * 95
            h = 20 + ((seed >> (index % 8)) & 0x3F)
            draw.rectangle((x, 760 - h, x + 40, 760), fill=(55 + index * 3, 62, 70))
        return image

    def _overlay(
        self,
        image: Image.Image,
        article: Article,
        theme: str | None = None,
        seed: int | None = None,
    ) -> Image.Image:
        theme = theme or self._infer_theme(article)
        seed = self._content_seed(article, theme) if seed is None else seed
        profile = self._theme_profile(theme)
        layouts = list(profile.get("layouts") or ("bottom_bar",))
        layout = str(layouts[seed % len(layouts)])
        accent = tuple(profile.get("accent") or (221, 69, 55))  # type: ignore[arg-type]
        brand = str(self.cover.get("brand") or profile.get("brand") or "热点观察")
        # optional brand can be forced empty via config
        if self.cover.get("brand") == "":
            brand = ""

        canvas = image.convert("RGBA")
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        title_font = self._font(56, bold=True)
        label_font = self._font(22, bold=False)
        tag_font = self._font(20, bold=False)
        title_lines = self._wrap_text(draw, article.title, title_font, 1000)[:3]
        theme_label = theme if theme != "综合" else "热点"

        if layout == "left_panel":
            draw.rectangle((0, 0, 430, 800), fill=(10, 14, 18, 210))
            draw.rectangle((430, 0, 444, 800), fill=(*accent, 255))
            draw.rounded_rectangle((48, 70, 190, 112), radius=18, fill=(*accent, 230))
            draw.text((62, 78), theme_label, font=tag_font, fill=(15, 23, 42, 255))
            if brand:
                draw.text((48, 140), brand, font=label_font, fill=(250, 204, 21, 255))
            y = 210 if brand else 160
            for line in self._wrap_text(draw, article.title, title_font, 340)[:5]:
                draw.text((48, y), line, font=title_font, fill=(255, 255, 255, 255))
                y += 68
        elif layout == "top_banner":
            draw.rectangle((0, 0, 1200, 250), fill=(8, 12, 16, 210))
            draw.rectangle((0, 250, 1200, 258), fill=(*accent, 255))
            if brand:
                draw.text((48, 36), f"{brand} · {theme_label}", font=label_font, fill=(250, 204, 21, 255))
            else:
                draw.text((48, 36), theme_label, font=label_font, fill=(*accent, 255))
            y = 88
            for line in title_lines:
                draw.text((48, y), line, font=title_font, fill=(255, 255, 255, 255))
                y += 64
        elif layout == "corner_card":
            draw.rounded_rectangle((36, 470, 760, 760), radius=28, fill=(10, 14, 18, 220))
            draw.rounded_rectangle((36, 470, 52, 760), radius=8, fill=(*accent, 255))
            draw.rounded_rectangle((60, 496, 180, 532), radius=14, fill=(*accent, 230))
            draw.text((74, 502), theme_label, font=tag_font, fill=(15, 23, 42, 255))
            y = 560
            for line in self._wrap_text(draw, article.title, title_font, 640)[:3]:
                draw.text((60, y), line, font=title_font, fill=(255, 255, 255, 255))
                y += 66
            if brand:
                draw.text((60, 710), brand, font=label_font, fill=(203, 213, 225, 255))
        elif layout == "minimal_caption":
            draw.rectangle((0, 640, 1200, 800), fill=(8, 12, 16, 180))
            draw.rectangle((48, 656, 120, 664), fill=(*accent, 255))
            y = 680
            for line in self._wrap_text(draw, article.title, title_font, 1100)[:2]:
                draw.text((48, y), line, font=title_font, fill=(255, 255, 255, 255))
                y += 58
        elif layout == "gradient_caption":
            for i in range(320):
                alpha = int(18 + i * 0.72)
                y = 800 - 320 + i
                draw.line((0, y, 1200, y), fill=(8, 12, 16, min(alpha, 230)))
            draw.ellipse((980, 560, 1280, 860), fill=(*accent, 50))
            draw.rounded_rectangle((48, 560, 170, 600), radius=16, fill=(*accent, 235))
            draw.text((62, 568), theme_label, font=tag_font, fill=(15, 23, 42, 255))
            if brand:
                draw.text((186, 568), brand, font=label_font, fill=(250, 204, 21, 255))
            y = 630
            for line in title_lines:
                draw.text((48, y), line, font=title_font, fill=(255, 255, 255, 255))
                y += 66
        else:  # bottom_bar diversified
            bar_h = 280 + (seed % 3) * 20
            draw.rectangle((0, 800 - bar_h, 1200, 800), fill=(12, 17, 20, 214))
            draw.rectangle((0, 800 - bar_h, 12 + (seed % 3) * 4, 800), fill=(*accent, 255))
            if brand:
                draw.text((58, 800 - bar_h + 28), f"{brand} · {theme_label}", font=label_font, fill=(250, 204, 21, 255))
            else:
                draw.text((58, 800 - bar_h + 28), theme_label, font=label_font, fill=(*accent, 255))
            y = 800 - bar_h + 78
            for line in title_lines:
                draw.text((58, y), line, font=title_font, fill=(255, 255, 255, 255))
                y += 68

        return Image.alpha_composite(canvas, overlay).convert("RGB")

    def _font(self, size: int, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Load a CJK-capable font. Never silently fall back for Chinese titles."""
        configured = resolve_path(self.config_dir, self.cover.get("font_path"))
        assets = Path(__file__).resolve().parent / "assets" / "fonts"
        windows = Path("C:/Windows/Fonts")
        candidates: list[Path] = []
        if configured:
            candidates.append(configured)
        candidates.extend(
            [
                assets / ("NotoSansSC-Bold.otf" if bold else "NotoSansSC-Regular.otf"),
                assets / ("SourceHanSansSC-Bold.otf" if bold else "SourceHanSansSC-Regular.otf"),
                assets / "NotoSansSC-Regular.otf",
                assets / "wqy-microhei.ttc",
                assets / "wqy-zenhei.ttc",
            ]
        )
        candidates.extend(
            [
                windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
                windows / "msyh.ttc",
                windows / "msyhbd.ttc",
                windows / "simhei.ttf",
                windows / "simsun.ttc",
                windows / "simkai.ttf",
                windows / "Deng.ttf",
                windows / "Dengb.ttf",
            ]
        )
        candidates.extend(
            [
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK.ttc"),
                Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/noto/NotoSansSC-Bold.otf" if bold else "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
                Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
                Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
                Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
            ]
        )
        scan_roots = [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
            assets,
        ]
        keywords = ("noto", "cjk", "sourcehan", "source-han", "wqy", "microhei", "zenhei", "uming", "ukai", "droid", "msyh", "simhei", "simsun", "deng")
        for root in scan_roots:
            if not root.is_dir():
                continue
            try:
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in {".ttf", ".otf", ".ttc", ".otc"}:
                        continue
                    name = path.name.lower()
                    if any(key in name for key in keywords):
                        candidates.append(path)
            except OSError:
                continue

        seen: set[str] = set()
        for candidate in candidates:
            if candidate is None:
                continue
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if not candidate.is_file():
                continue
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                try:
                    return ImageFont.truetype(str(candidate), size=size, index=0)
                except OSError:
                    continue
        LOG.error(
            "No CJK font found for cover overlay. Install fonts-noto-cjk / fonts-wqy-microhei "
            "or place NotoSansSC-*.otf under assets/fonts/"
        )
        return ImageFont.load_default(size=size)

    @staticmethod
    def _wrap_text(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        lines: list[str] = []
        current = ""
        for char in text:
            candidate = current + char
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines



def save_article(article: Article, config: dict[str, Any], config_dir: Path, path: Path | None) -> Path:
    if path is None:
        draft_dir = resolve_path(config_dir, config["upload"]["draft_dir"])
        assert draft_dir is not None
        draft_dir.mkdir(parents=True, exist_ok=True)
        path = draft_dir / f"{utc_stamp()}-{slug(article.title)}.json"
    else:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(article.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_topics(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def load_ledger(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return set(payload.get("completed_topics", []))


def save_ledger(path: Path, completed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"completed_topics": sorted(completed), "updated_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("session-check", help="verify the configured Toutiao Cookie over HTTP")

    generate = commands.add_parser("generate", help="generate an article JSON draft")
    generate.add_argument("--topic")
    generate.add_argument("--out", type=Path)

    publish = commands.add_parser("publish", help="upload a saved article draft")
    publish.add_argument("draft", type=Path)
    add_publish_options(publish)

    run = commands.add_parser("run", help="generate and upload one article")
    run.add_argument("--topic")
    run.add_argument("--out", type=Path)
    add_publish_options(run)

    batch = commands.add_parser("batch", help="process every uncompleted topic in a text file")
    batch.add_argument("--topics-file", type=Path)
    add_publish_options(batch)
    return parser


def add_publish_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("draft", "publish"))
    parser.add_argument("--cover", type=Path)
    parser.add_argument("--dry-run", action="store_true")


def publish_article(
    config: dict[str, Any],
    config_dir: Path,
    article: Article,
    args: argparse.Namespace,
) -> dict[str, Any]:
    mode = args.mode or str(config["upload"].get("mode", "draft"))
    configured_cover = resolve_path(config_dir, config["upload"].get("cover_path"))
    cover = args.cover.expanduser().resolve() if args.cover else configured_cover
    with ToutiaoProtocolClient(config, config_dir) as publisher:
        return publisher.publish(article, mode, cover, bool(args.dry_run))


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config_path = args.config.expanduser().resolve()
    if not config_path.is_file():
        print(f"error: config file does not exist: {config_path}", file=sys.stderr)
        return 2
    config = load_toml(config_path)
    config_dir = config_path.parent

    try:
        if args.command == "session-check":
            with ToutiaoProtocolClient(config, config_dir) as publisher:
                print(json.dumps(publisher.check_session(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "generate":
            topic = args.topic or str(config["content"]["default_topic"])
            article = ArticleGenerator(config).generate(topic)
            path = save_article(article, config, config_dir, args.out)
            print(path)
            return 0

        if args.command == "publish":
            article = Article.load(args.draft.expanduser().resolve())
            print(json.dumps(publish_article(config, config_dir, article, args), ensure_ascii=False, indent=2))
            return 0

        if args.command == "run":
            topic = args.topic or str(config["content"]["default_topic"])
            article = ArticleGenerator(config).generate(topic)
            draft = save_article(article, config, config_dir, args.out)
            LOG.info("Draft saved: %s", draft)
            print(json.dumps(publish_article(config, config_dir, article, args), ensure_ascii=False, indent=2))
            return 0

        if args.command == "batch":
            batch_config = config["batch"]
            topics_path = (
                args.topics_file.expanduser().resolve()
                if args.topics_file
                else resolve_path(config_dir, batch_config["topics_file"])
            )
            ledger_path = resolve_path(config_dir, batch_config["ledger_file"])
            assert topics_path is not None and ledger_path is not None
            topics = read_topics(topics_path)
            completed = load_ledger(ledger_path)
            generator = ArticleGenerator(config)
            delay = max(0, int(batch_config.get("delay_seconds", 120)))
            for topic in topics:
                if topic in completed:
                    LOG.info("Skipping completed topic: %s", topic)
                    continue
                try:
                    article = generator.generate(topic)
                    save_article(article, config, config_dir, None)
                    report = publish_article(config, config_dir, article, args)
                    print(json.dumps(report, ensure_ascii=False))
                    completed.add(topic)
                    save_ledger(ledger_path, completed)
                except Exception:
                    LOG.exception("Topic failed: %s", topic)
                    if not bool(batch_config.get("continue_on_error", True)):
                        raise
                if delay:
                    time.sleep(delay)
            return 0
    except (PublisherError, OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
