#!/usr/bin/env python3
"""Concurrent multi-platform hot-topic aggregation and classification."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable


LOG = logging.getLogger("toutiao-dashboard.hot-topics")

SOURCE_NAMES = {
    "toutiao": "头条热榜",
    "baidu": "百度热搜",
    "weibo": "微博热搜",
    "zhihu": "知乎热榜",
    "douyin": "抖音热点",
    "bilibili": "B站热门",
    "csdn": "CSDN 热榜",
    "acfun": "AcFun 排行",
    "hackernews": "Hacker News",
}

DEFAULT_SOURCES = list(SOURCE_NAMES)
CATEGORY_ORDER = ["科技", "财经", "社会", "娱乐", "体育", "健康", "教育", "汽车", "游戏", "国际", "生活", "其他"]

CATEGORY_KEYWORDS = {
    "科技": (
        "ai", "人工智能", "大模型", "机器人", "芯片", "手机", "电脑", "软件", "互联网",
        "算法", "开源", "编程", "程序员", "数码", "苹果", "华为", "小米", "微软", "谷歌",
        "openai", "github", "linux", "科技", "智能体", "数据库", "服务器",
    ),
    "财经": (
        "a股", "港股", "美股", "股市", "股票", "基金", "银行", "金融", "经济", "房价",
        "楼市", "人民币", "美元", "黄金", "油价", "关税", "财报", "融资", "投资", "上市",
        "市值", "消费", "降息", "涨价", "降价",
    ),
    "社会": (
        "警方", "法院", "通报", "事故", "暴雨", "台风", "高温", "地震", "火灾", "救援",
        "男子", "女子", "老人", "儿童", "医院", "小区", "城市", "辟谣", "回应", "去世",
        "失联", "交通", "公共", "应急", "谣言", "涉灾",
    ),
    "娱乐": (
        "电影", "电视剧", "综艺", "票房", "演员", "导演", "明星", "歌手", "演唱会", "新片",
        "娱乐", "周星驰", "婚变", "官宣", "直播", "短剧",
    ),
    "体育": (
        "世界杯", "足球", "篮球", "nba", "cba", "中超", "女足", "男足", "比赛", "冠军",
        "奥运", "全运会", "网球", "羽毛球", "乒乓球", "运动员", "哈兰德", "梅西", "赛事",
    ),
    "健康": (
        "健康", "医生", "疾病", "药品", "医学", "减肥", "睡眠", "营养", "疫苗", "感染",
        "癌症", "急救", "心理", "食品安全", "养生",
    ),
    "教育": (
        "大学", "高校", "学校", "学生", "老师", "教师", "高考", "中考", "考研", "录取",
        "学位", "教育", "校园", "课程", "留学", "毕业",
    ),
    "汽车": (
        "汽车", "车企", "新能源车", "电动车", "自动驾驶", "智驾", "特斯拉", "比亚迪", "续航",
        "车主", "车型", "充电桩",
    ),
    "游戏": (
        "游戏", "电竞", "玩家", "steam", "主机", "手游", "网游", "任天堂", "xbox",
        "playstation", "育碧", "暴雪",
    ),
    "国际": (
        "美国", "俄罗斯", "乌克兰", "日本", "韩国", "印度", "欧洲", "欧盟", "英国", "法国",
        "德国", "以色列", "伊朗", "联合国", "外交", "国际", "总统", "首相", "战争", "胡塞",
    ),
    "生活": (
        "旅行", "旅游", "美食", "餐厅", "穿搭", "家居", "宠物", "天气", "上班", "职场",
        "婚姻", "家庭", "育儿", "购物", "假期", "生活", "外卖", "咖啡",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def numeric(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip().lower().replace(",", "")
    match = re.search(r"([\d.]+)\s*([w万亿k]?)", text)
    if not match:
        return 0
    amount = float(match.group(1))
    multiplier = {"w": 10_000, "万": 10_000, "亿": 100_000_000, "k": 1_000}.get(
        match.group(2), 1
    )
    return int(amount * multiplier)


def topic_key(title: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", title.lower(), flags=re.UNICODE)
    return normalized or title.lower().strip()


def content_seed(*parts: object) -> int:
    blob = "|".join(str(part or "") for part in parts)
    return int.from_bytes(hashlib.sha256(blob.encode("utf-8")).digest()[:8], "big")


def suggest_writing_angles(
    title: str,
    category: str = "",
    sources: list[str] | None = None,
    hot_value: int = 0,
    label: str = "",
) -> list[str]:
    """Generate diversified writing angles from topic content (no fixed template)."""
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    category = str(category or "其他").strip() or "其他"
    sources = [str(item) for item in (sources or [])]
    label = str(label or "")
    text = title.lower()
    seed = content_seed(title, category, ",".join(sources), hot_value, label)

    signals: list[str] = []
    rules = (
        (("如何", "怎么", "怎样", "指南", "教程", "方法", "技巧", "步骤"), "howto"),
        (("为什么", "原因", "为何", "背后"), "why"),
        (("公布", "发布", "官宣", "上线", "推出", "亮相", "开售"), "launch"),
        (("暴涨", "暴跌", "大涨", "大跌", "飙升", "跳水", "涨停", "跌停"), "market"),
        (("争议", "争议", "翻车", "质疑", "回应", "辟谣", "造假"), "debate"),
        (("对比", "pk", "vs", "哪个好", "区别", "比较"), "compare"),
        (("第一", "榜", "排名", "top", "最强", "之最"), "ranking"),
        (("政策", "法规", "条例", "新规", "监管", "立案", "通报"), "policy"),
        (("去世", "身亡", "事故", "遇难", "伤亡", "地震", "台风", "暴雨"), "breaking"),
        (("人工智能", "ai", "大模型", "芯片", "手机", "汽车", "新能源"), "tech_product"),
        (("高考", "中考", "考研", "大学", "学校", "学生"), "education"),
        (("电影", "电视剧", "综艺", "演唱会", "明星", "票房"), "entertainment"),
        (("比赛", "冠军", "决赛", "世界杯", "奥运", "nba"), "sports"),
    )
    for words, tag in rules:
        if any(word in text for word in words):
            signals.append(tag)
    if "跨平台" in label or len(set(sources)) >= 2:
        signals.append("cross_platform")
    if hot_value and int(hot_value) >= 1_000_000:
        signals.append("viral")
    if not signals:
        signals.append("general")

    category_angles: dict[str, tuple[str, ...]] = {
        "科技": (
            "把复杂技术翻译成普通人能理解的产品体验变化，并给出选择建议",
            "从效率与成本出发，解释这项技术会如何改变工作流",
            "拆解技术热词背后的真实能力边界，避免跟风误判",
        ),
        "财经": (
            "聚焦普通投资者/消费者最关心的价格、风险与时间窗口",
            "用因果链说明事件如何传导到就业、消费与资产配置",
            "区分短期情绪和长期基本面，给出可执行的观察清单",
        ),
        "社会": (
            "还原事件脉络，强调公共规则、个人权益与可借鉴经验",
            "从普通人生活场景切入，说明此事为何值得关注",
            "梳理多方立场与信息缺口，避免情绪化复述",
        ),
        "健康": (
            "给出可自查的风险信号与就医/生活调整建议，避免恐吓式表达",
            "把专业结论转成日常可执行习惯，并标明适用边界",
            "澄清常见误区，帮助读者建立正确健康决策框架",
        ),
        "教育": (
            "面向家长/学生给出阶段化行动建议与资源筛选标准",
            "解释政策或考试变化对升学路径的实际影响",
            "用案例化步骤帮助学生避开常见决策坑",
        ),
        "娱乐": (
            "从作品/人设/产业逻辑解读热度，而不是堆砌八卦",
            "分析为何能出圈，以及对内容创作的启发",
            "平衡热度与信息密度，给读者清晰的观感判断",
        ),
        "体育": (
            "用赛况关键节点复盘胜负手，并延伸到战术与状态管理",
            "把专业数据转成观众看得懂的故事线",
            "关注选手状态、赛制规则与下一阶段看点",
        ),
        "汽车": (
            "围绕购车/用车决策，比较性能、成本、安全与售后",
            "解释技术卖点对真实通勤场景的价值",
            "提醒读者关注续航/智驾边界与使用成本",
        ),
        "国际": (
            "用清晰时间线说明事件进展，并解释对中国读者的关联",
            "梳理利益格局与可能走向，避免阴谋论式推断",
            "补充背景知识，帮助理解新闻标题之外的深层影响",
        ),
        "生活": (
            "提供可立刻尝试的生活方案，并写明预算与注意事项",
            "从真实场景痛点出发，给出取舍建议",
            "把热点转化成提升生活质量的具体清单",
        ),
        "游戏": (
            "从玩法、体验与社区反馈切入，帮助读者判断是否值得投入时间",
            "拆解更新/赛事为何引发讨论，以及其对玩家的实际影响",
            "平衡娱乐性与信息量，给出入门或进阶建议",
        ),
        "其他": (
            "抓住标题中的核心矛盾，用问答结构回答读者最想知道的三点",
            "先给结论，再补背景、影响与行动建议",
            "用对比和清单让信息更快被吸收",
        ),
    }

    signal_angles: dict[str, tuple[str, ...]] = {
        "howto": (
            "采用「问题-步骤-避坑」结构，给出可照做的操作路径",
            "把方法拆成新手版与进阶版，明确适用人群",
        ),
        "why": (
            "先给结论再追问原因，区分表层原因与深层机制",
            "用因果图方式解释多方动机，避免单一归因",
        ),
        "launch": (
            "解读发布信息对用户权益、价格与替代方案的影响",
            "对比新旧方案差异，提炼「现在要不要跟进」的判断标准",
        ),
        "market": (
            "解释波动触发因素，并给出观察指标而非盲目预测涨跌",
            "帮助读者理解风险暴露，列出稳健应对动作",
        ),
        "debate": (
            "并列展示争议双方核心论据，指出证据强弱与信息缺口",
            "把情绪热点转成理性判断框架，提醒读者识别片面信息",
        ),
        "compare": (
            "建立统一评价维度做横向对比，最后给出场景化选择建议",
            "避免站队式结论，按预算/目标/风险偏好分类推荐",
        ),
        "ranking": (
            "拆解榜单规则与代表性样本，避免把排名当成绝对优劣",
            "从榜单变化看趋势，补充被忽略但重要的选项",
        ),
        "policy": (
            "翻译政策条款为普通人能懂的权利义务变化",
            "说明谁受益、谁承压，以及近期需要准备什么",
        ),
        "breaking": (
            "以事实时间线为主，克制情绪渲染，补充求助与防范信息",
            "聚焦公共安全启示与读者可采取的防护动作",
        ),
        "tech_product": (
            "从体验、成本、生态兼容三方面评估是否值得关注",
            "把参数热词还原成真实使用场景收益",
        ),
        "education": (
            "给出阶段任务清单与常见误区，强调长期规划而非焦虑营销",
        ),
        "entertainment": (
            "结合作品完成度与传播机制，解释热度可持续性",
        ),
        "sports": (
            "抓住关键回合/数据转折，帮助读者看懂比赛逻辑",
        ),
        "cross_platform": (
            "综合多平台热议点，提炼共识与分歧，形成更完整叙事",
        ),
        "viral": (
            "解释为何突然爆火，并提醒信息过载下的核实习惯",
        ),
        "general": (
            "围绕读者决策需求组织内容：发生了什么、为何重要、怎么做",
        ),
    }

    pool: list[str] = []
    for sig in signals:
        pool.extend(signal_angles.get(sig, ()))
    pool.extend(category_angles.get(category, category_angles["其他"]))
    # content-specific lead-in derived from title keywords
    key_bits = [part for part in re.split(r"[：:，,。！？\s]+", title) if 1 < len(part) <= 12][:3]
    if key_bits:
        focus = "、".join(key_bits)
        pool.insert(0, f"紧扣「{focus}」这一核心信息，先给读者结论，再展开背景、影响与建议")
        pool.append(f"以「{focus}」为线索，对照普通人最关心的成本、风险与机会来写")

    # de-dupe preserve order
    unique: list[str] = []
    seen: set[str] = set()
    for item in pool:
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            unique.append(clean)
    if not unique:
        unique = ["结合事件背景、读者影响和可执行建议展开，避免空泛复述"]

    # rotate by seed for diversity, keep top 3
    if len(unique) > 1:
        start = seed % len(unique)
        unique = unique[start:] + unique[:start]
    return unique[:3]


def suggest_writing_angle(
    title: str,
    category: str = "",
    sources: list[str] | None = None,
    hot_value: int = 0,
    label: str = "",
) -> str:
    return suggest_writing_angles(title, category, sources, hot_value, label)[0]


class HotTopicService:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config.get("hot_topics", {})
        configured = self.config.get("sources", DEFAULT_SOURCES)
        self.sources = [
            str(item).strip().lower()
            for item in configured
            if str(item).strip().lower() in SOURCE_NAMES
        ] or DEFAULT_SOURCES.copy()
        self.lock = threading.Lock()
        self.cached_at = 0.0
        self.cache: list[dict[str, Any]] = []
        self.source_cache: dict[str, list[dict[str, Any]]] = {}
        self.source_status: dict[str, dict[str, Any]] = {}
        self.refreshed_at: str | None = None
        self._refreshing = False
        self._bg_stop = threading.Event()
        self._bg_thread: threading.Thread | None = None
        if bool(self.config.get("background_refresh", True)):
            self.start_background_refresh()

    def start_background_refresh(self) -> None:
        if self._bg_thread and self._bg_thread.is_alive():
            return
        interval = max(20, int(self.config.get("refresh_interval_seconds", 45)))

        def loop() -> None:
            try:
                self.fetch(force=True)
            except Exception:
                LOG.exception("Initial hot-topic background refresh failed")
            while not self._bg_stop.wait(interval):
                try:
                    self.fetch(force=True)
                except Exception:
                    LOG.exception("Hot-topic background refresh failed")

        self._bg_thread = threading.Thread(target=loop, name="hot-topics-bg", daemon=True)
        self._bg_thread.start()
        LOG.info("Hot-topic background refresh started (every %ss)", interval)

    def stop_background_refresh(self) -> None:
        self._bg_stop.set()

    def fetch(self, force: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            ttl = max(10, int(self.config.get("cache_seconds", 30)))
            if self.cache and not force and time.monotonic() - self.cached_at < ttl:
                return copy.deepcopy(self.cache)
            if self._refreshing and self.cache and not force:
                return copy.deepcopy(self.cache)
            self._refreshing = True
            prev_source_cache = copy.deepcopy(self.source_cache)
            prev_source_status = copy.deepcopy(self.source_status)

        source_rows: dict[str, list[dict[str, Any]]] = {}
        source_status: dict[str, dict[str, Any]] = {}
        source_cache_updates: dict[str, list[dict[str, Any]]] = {}
        workers = max(1, min(len(self.sources), int(self.config.get("max_workers", 9))))
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hot-source") as executor:
                futures = {executor.submit(self._fetch_source, source): source for source in self.sources}
                for future in as_completed(futures):
                    source = futures[future]
                    started = time.monotonic()
                    try:
                        rows, elapsed_ms = future.result()
                        if not rows:
                            raise RuntimeError("榜单未返回有效条目")
                        source_cache_updates[source] = rows
                        source_rows[source] = rows
                        source_status[source] = {
                            "id": source,
                            "name": SOURCE_NAMES[source],
                            "status": "ok",
                            "count": len(rows),
                            "latency_ms": elapsed_ms,
                            "fetched_at": utc_now(),
                            "error": "",
                        }
                    except Exception as exc:
                        stale = copy.deepcopy(prev_source_cache.get(source, []))
                        if stale:
                            source_rows[source] = stale
                        source_status[source] = {
                            "id": source,
                            "name": SOURCE_NAMES[source],
                            "status": "stale" if stale else "error",
                            "count": len(stale),
                            "latency_ms": int((time.monotonic() - started) * 1000),
                            "fetched_at": prev_source_status.get(source, {}).get("fetched_at"),
                            "error": f"{type(exc).__name__}: {exc}"[:240],
                        }
                        LOG.warning("Hot source %s failed: %s", source, exc)

            aggregated = self._aggregate(source_rows)
            if not aggregated:
                aggregated = self._fallback()
            refreshed_at = utc_now()
            with self.lock:
                self.source_cache.update(source_cache_updates)
                self.source_status.update(source_status)
                self.cache = aggregated
                self.cached_at = time.monotonic()
                self.refreshed_at = refreshed_at
                return copy.deepcopy(self.cache)
        finally:
            with self.lock:
                self._refreshing = False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            topics = copy.deepcopy(self.cache)
            refreshed_at = self.refreshed_at
            source_status = copy.deepcopy(self.source_status)
            sources_list = list(self.sources)
            ttl = max(10, int(self.config.get("cache_seconds", 30)))
            age = time.monotonic() - self.cached_at if self.cached_at else None
        sources = [
            copy.deepcopy(
                source_status.get(
                    source,
                    {
                        "id": source,
                        "name": SOURCE_NAMES[source],
                        "status": "pending",
                        "count": 0,
                        "latency_ms": 0,
                        "fetched_at": None,
                        "error": "",
                    },
                )
            )
            for source in sources_list
        ]
        categories = [
            {"name": category, "count": sum(1 for topic in topics if topic["category"] == category)}
            for category in CATEGORY_ORDER
            if any(topic["category"] == category for topic in topics)
        ]
        return {
            "topics": topics,
            "sources": sources,
            "categories": categories,
            "total": len(topics),
            "healthy_sources": sum(1 for source in sources if source["status"] == "ok"),
            "refreshed_at": refreshed_at or utc_now(),
            "cache_seconds": ttl,
            "cache_age_seconds": None if age is None else round(max(0.0, age), 1),
            "realtime": True,
        }

    def _fetch_source(self, source: str) -> tuple[list[dict[str, Any]], int]:
        fetcher: Callable[[], list[dict[str, Any]]] = getattr(self, f"_fetch_{source}")
        started = time.monotonic()
        rows = fetcher()
        limit = max(5, int(self.config.get("per_source_limit", 30)))
        return rows[:limit], int((time.monotonic() - started) * 1000)

    def _request_json(self, url: str, referer: str = "") -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, headers=headers)
        timeout = max(3, float(self.config.get("timeout_seconds", 12)))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("响应 JSON 不是对象")
        return payload

    def _row(
        self,
        source: str,
        rank: int,
        title: Any,
        url: Any = "",
        hot_value: Any = 0,
        label: Any = "实时",
        hint: Any = "",
    ) -> dict[str, Any] | None:
        clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not clean_title:
            return None
        return {
            "source_key": source,
            "source": SOURCE_NAMES[source],
            "source_rank": rank,
            "title": clean_title,
            "url": str(url or ""),
            "hot_value": numeric(hot_value),
            "label": str(label or "实时"),
            "category_hint": str(hint or ""),
        }

    def _fetch_toutiao(self) -> list[dict[str, Any]]:
        endpoint = str(
            self.config.get(
                "toutiao_endpoint",
                self.config.get(
                    "endpoint",
                    "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
                ),
            )
        )
        payload = self._request_json(endpoint, "https://www.toutiao.com/")
        rows = payload.get("data") or payload.get("list") or []
        result = []
        for rank, item in enumerate(rows, start=1):
            row = self._row(
                "toutiao",
                rank,
                item.get("Title") or item.get("title"),
                item.get("Url") or item.get("url"),
                item.get("HotValue") or item.get("hot_value") or item.get("hotValue"),
                item.get("Label") or item.get("label") or "实时",
            )
            if row:
                result.append(row)
        return result

    def _fetch_baidu(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://top.baidu.com/api/board?platform=wise&tab=realtime",
            "https://top.baidu.com/board?tab=realtime",
        )
        cards = payload.get("data", {}).get("cards", [])
        items: list[dict[str, Any]] = []
        for card in cards:
            for item in card.get("content", []):
                nested = item.get("content")
                if isinstance(nested, list):
                    items.extend(row for row in nested if isinstance(row, dict))
                elif isinstance(item, dict):
                    items.append(item)
        result = []
        for rank, item in enumerate(items, start=1):
            label = item.get("newHotName") or item.get("labelTagName") or "实时"
            row = self._row(
                "baidu",
                int(item.get("index") or rank),
                item.get("word") or item.get("title"),
                item.get("url"),
                item.get("hotScore") or item.get("hotValue"),
                label,
            )
            if row:
                result.append(row)
        return result


    def _fetch_weibo(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://weibo.com/ajax/side/hotSearch",
            "https://weibo.com/",
        )
        data = payload.get("data") or {}
        rows = data.get("realtime") or data.get("band_list") or []
        result = []
        for rank, item in enumerate(rows, start=1):
            word = item.get("word") or item.get("note") or item.get("title")
            url = item.get("word_scheme") or item.get("url")
            if not url and word:
                url = f"https://s.weibo.com/weibo?q={urllib_quote(str(word))}"
            row = self._row(
                "weibo",
                int(item.get("rank") or rank),
                word,
                url,
                item.get("num") or item.get("raw_hot") or item.get("hot_value"),
                item.get("label_name") or item.get("icon_desc") or "热搜",
                "社会 热点 实时",
            )
            if row:
                result.append(row)
        return result

    def _fetch_zhihu(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50&desktop=true",
            "https://www.zhihu.com/hot",
        )
        rows = payload.get("data") or []
        result = []
        for rank, item in enumerate(rows, start=1):
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            title_area = target.get("title_area") if isinstance(target.get("title_area"), dict) else {}
            metrics = target.get("metrics_area") if isinstance(target.get("metrics_area"), dict) else {}
            link = target.get("link") if isinstance(target.get("link"), dict) else {}
            title = (
                title_area.get("text")
                or target.get("title")
                or item.get("title")
                or item.get("target_title")
            )
            heat = metrics.get("text") or item.get("detail_text") or item.get("fire_text") or ""
            url = str(link.get("url") or target.get("url") or "")
            if not url and target.get("id"):
                url = f"https://www.zhihu.com/question/{target.get('id')}"
            row = self._row(
                "zhihu",
                rank,
                title,
                url,
                heat,
                "热榜",
                "讨论 观点 深度",
            )
            if row:
                result.append(row)
        return result

    def _fetch_douyin(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
            "https://www.douyin.com/",
        )
        rows = payload.get("word_list") or payload.get("data", {}).get("word_list") or []
        result = []
        for rank, item in enumerate(rows, start=1):
            word = item.get("word") or item.get("sentence")
            url = f"https://www.douyin.com/search/{urllib_quote(str(word or ''))}"
            row = self._row(
                "douyin",
                int(item.get("position") or rank),
                word,
                url,
                item.get("hot_value") or item.get("hotValue"),
                item.get("event_time") and "新" or "实时",
            )
            if row:
                result.append(row)
        return result

    def _fetch_bilibili(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
            "https://www.bilibili.com/",
        )
        rows = payload.get("data", {}).get("list", [])
        result = []
        for rank, item in enumerate(rows, start=1):
            bvid = str(item.get("bvid") or "")
            row = self._row(
                "bilibili",
                rank,
                item.get("title"),
                f"https://www.bilibili.com/video/{bvid}" if bvid else item.get("short_link_v2"),
                item.get("stat", {}).get("view"),
                item.get("tname") or "热门",
                item.get("tname") or item.get("tnamev2"),
            )
            if row:
                result.append(row)
        return result

    def _fetch_csdn(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://blog.csdn.net/phoenix/web/blog/hot-rank?page=0&pageSize=30&type=",
            "https://blog.csdn.net/rank/list",
        )
        rows = payload.get("data") or []
        result = []
        for rank, item in enumerate(rows, start=1):
            row = self._row(
                "csdn",
                rank,
                item.get("articleTitle"),
                item.get("articleDetailUrl"),
                item.get("hotRankScore") or item.get("viewCount"),
                "技术",
                "科技 编程 软件",
            )
            if row:
                result.append(row)
        return result

    def _fetch_acfun(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://www.acfun.cn/rest/pc-direct/rank/channel?"
            "channelId=0&subChannelId=&rankLimit=30&rankPeriod=DAY",
            "https://www.acfun.cn/rank/list/",
        )
        rows = payload.get("rankList") or []
        result = []
        for rank, item in enumerate(rows, start=1):
            channel = item.get("channel") if isinstance(item.get("channel"), dict) else {}
            row = self._row(
                "acfun",
                rank,
                item.get("contentTitle") or item.get("title"),
                item.get("shareUrl") or item.get("picShareUrl"),
                item.get("viewCount"),
                channel.get("name") or "热门",
                f"{channel.get('parentName', '')} {channel.get('name', '')}",
            )
            if row:
                result.append(row)
        return result

    def _fetch_hackernews(self) -> list[dict[str, Any]]:
        payload = self._request_json("https://hn.algolia.com/api/v1/search?tags=front_page")
        rows = sorted(
            payload.get("hits") or [],
            key=lambda item: numeric(item.get("points")),
            reverse=True,
        )
        result = []
        for rank, item in enumerate(rows, start=1):
            object_id = str(item.get("objectID") or "")
            row = self._row(
                "hackernews",
                rank,
                item.get("title") or item.get("story_title"),
                item.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
                item.get("points"),
                "全球科技",
                "科技 互联网 编程",
            )
            if row:
                result.append(row)
        return result

    def _aggregate(self, source_rows: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        per_source_limit = max(5, int(self.config.get("per_source_limit", 30)))
        for source in self.sources:
            for row in source_rows.get(source, []):
                key = topic_key(row["title"])
                rank_score = max(1, per_source_limit + 1 - int(row["source_rank"]))
                if key not in groups:
                    groups[key] = {
                        **row,
                        "score": rank_score,
                        "source_keys": [source],
                        "source_names": [row["source"]],
                        "source_details": [
                            {
                                "id": source,
                                "name": row["source"],
                                "rank": row["source_rank"],
                                "url": row["url"],
                            }
                        ],
                    }
                    continue
                group = groups[key]
                group["score"] += rank_score + 20
                group["hot_value"] = max(group["hot_value"], row["hot_value"])
                group["source_keys"].append(source)
                group["source_names"].append(row["source"])
                group["source_details"].append(
                    {
                        "id": source,
                        "name": row["source"],
                        "rank": row["source_rank"],
                        "url": row["url"],
                    }
                )
                if int(row["source_rank"]) < int(group["source_rank"]):
                    group["url"] = row["url"]
                    group["label"] = row["label"]
                    group["source_rank"] = row["source_rank"]

        ordered = sorted(
            groups.values(),
            key=lambda item: (int(item["score"]), len(item["source_keys"]), item["hot_value"]),
            reverse=True,
        )
        limit = max(10, int(self.config.get("limit", 150)))
        result: list[dict[str, Any]] = []
        for rank, group in enumerate(ordered[:limit], start=1):
            category = self._classify(
                group["title"], group.get("category_hint", ""), group["source_keys"]
            )
            source_count = len(group["source_keys"])
            label = "跨平台" if source_count > 1 else group["label"]
            angles = suggest_writing_angles(
                group["title"],
                category,
                group["source_keys"],
                group.get("hot_value") or 0,
                label,
            )
            result.append(
                {
                    "id": hashlib.sha1(topic_key(group["title"]).encode("utf-8")).hexdigest()[:12],
                    "rank": rank,
                    "title": group["title"],
                    "url": group["url"],
                    "hot_value": group["hot_value"],
                    "score": group["score"],
                    "label": label,
                    "source": " / ".join(group["source_names"]),
                    "source_keys": group["source_keys"],
                    "source_count": source_count,
                    "source_details": group["source_details"],
                    "category": category,
                    "angle": angles[0],
                    "angles": angles,
                    "is_fallback": False,
                    "fetched_at": self.refreshed_at or utc_now(),
                }
            )
        return result

    @staticmethod
    def _classify(title: str, hint: str, sources: list[str]) -> str:
        text = f"{title} {hint}".lower()
        if "hackernews" in sources or "csdn" in sources:
            return "科技"
        for category in CATEGORY_ORDER:
            if category == "其他":
                continue
            if any(keyword in text for keyword in CATEGORY_KEYWORDS.get(category, ())):
                return category
        return "其他"

    @staticmethod
    def _fallback() -> list[dict[str, Any]]:
        titles = (
            ("科技", "人工智能应用进入精细化落地阶段"),
            ("生活", "普通人的数字效率工具正在发生哪些变化"),
            ("财经", "新消费趋势背后的理性选择"),
            ("社会", "城市公共服务中的技术创新"),
            ("生活", "年轻人重新审视工作与生活的边界"),
        )
        result = []
        for rank, (category, title) in enumerate(titles, start=1):
            angles = suggest_writing_angles(title, category, ["local"], 0, "备用")
            result.append(
                {
                    "id": hashlib.sha1(title.encode("utf-8")).hexdigest()[:12],
                    "rank": rank,
                    "title": title,
                    "url": "",
                    "hot_value": 0,
                    "score": 0,
                    "label": "备用",
                    "source": "本地选题",
                    "source_keys": ["local"],
                    "source_count": 1,
                    "source_details": [],
                    "category": category,
                    "angle": angles[0],
                    "angles": angles,
                    "is_fallback": True,
                    "fetched_at": utc_now(),
                }
            )
        return result


def urllib_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
