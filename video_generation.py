"""OpenAI-compatible asynchronous video generation."""

from __future__ import annotations

import base64
import json
import logging
import math
import sys
import re
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

import httpx

from net_utils import httpx_client, prefer_ipv4

from toutiao_publisher import Article, PublisherError, resolve_path, slug, utc_stamp


ProgressCallback = Callable[[str, int], None]
LOG = logging.getLogger("toutiao-video")


class VideoGenerator:
    def __init__(self, config: dict[str, Any], config_dir: Path) -> None:
        self.config = config
        self.config_dir = config_dir
        self.video = config.get("video", {})
        key_env = str(self.video.get("api_key_env") or "OPENAI_API_KEY")
        self.api_key = str(self.video.get("api_key") or os.getenv(key_env, "")).strip()
        if not self.api_key:
            raise PublisherError(f"Missing video API key environment variable: {key_env}")
        self.base_url = str(self.video.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.create_path = "/" + str(self.video.get("create_path") or "videos").strip("/")
        self.model = str(self.video.get("model") or "sora-2")
        self.poll_interval = max(1.0, float(self.video.get("poll_interval", 5)))
        self.timeout = max(30.0, float(self.video.get("timeout", 900)))
        self.max_download_bytes = int(self.video.get("max_download_bytes", 500 * 1024 * 1024))
        self.http_proxy = self._resolve_proxy()
        self.download_url_template = str(self.video.get("download_url_template") or "").strip()
        self.direct_connect_timeout = max(3.0, float(self.video.get("direct_connect_timeout", 8)))
        self.direct_timeout = max(15.0, float(self.video.get("direct_timeout", 90)))
        # multi-segment stitch: model clips are short; longer targets are composed
        self.segment_max_seconds = max(2, min(15, int(self.video.get("segment_max_seconds", 15) or 15)))
        self.stitch_enabled = bool(self.video.get("stitch_enabled", True))
        self.stitch_crossfade = max(0.0, min(1.5, float(self.video.get("stitch_crossfade", 0.35) or 0.35)))
        self.max_segments = max(1, min(8, int(self.video.get("max_segments", 6) or 6)))
        self.ffmpeg_bin = str(self.video.get("ffmpeg") or shutil.which("ffmpeg") or "ffmpeg")
        self.audio_enabled = bool(self.video.get("audio_enabled", True))
        # AI clips often include a silent AAC track; force TTS unless real audio is detected.
        self.audio_force_tts = bool(self.video.get("audio_force_tts", True))
        self.audio_silence_mean_db = float(self.video.get("audio_silence_mean_db", -45.0) or -45.0)
        self.tts_voice = str(self.video.get("tts_voice") or "zh-CN-XiaoxiaoNeural")
        self.tts_rate = str(self.video.get("tts_rate") or "+0%")
        self.tts_volume = str(self.video.get("tts_volume") or "+0%")
        self.bgm_volume = max(0.0, min(1.0, float(self.video.get("bgm_volume", 0.12) or 0.12)))

    def _resolve_proxy(self) -> str | None:
        for key in ("http_proxy", "https_proxy", "proxy", "all_proxy"):
            value = str(self.video.get(key) or "").strip()
            if value:
                return value
        for env_key in (
            "VIDEO_HTTP_PROXY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "https_proxy",
            "http_proxy",
            "all_proxy",
        ):
            value = str(os.getenv(env_key) or "").strip()
            if value:
                return value
        return None

    def generate(
        self,
        article: Article,
        guidance: str = "",
        *,
        duration: int | None = None,
        aspect_ratio: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> Path:
        target_seconds = max(2, int(duration or self.video.get("duration", 15)))
        ratio = str(aspect_ratio or self.video.get("aspect_ratio", "16:9"))
        size = self._size_for_ratio(ratio)
        segments = self._plan_segments(target_seconds)
        segments = self._attach_narration_lines(article, segments, guidance=guidance)
        LOG.info(
            "Auto video segments for '%s': total=%ss -> %s clips %s",
            article.title[:40],
            target_seconds,
            len(segments),
            [int(s['seconds']) for s in segments],
        )
        for idx, seg in enumerate(segments, 1):
            LOG.info(
                "Narration beat %s/%s role=%s: %s",
                idx,
                len(segments),
                seg.get("role"),
                str(seg.get("line") or "")[:60],
            )
        visual_lock = self._build_visual_lock(article, guidance, ratio)
        continuity = self._continuity_bible(article, guidance, ratio, visual_lock)

        if len(segments) == 1 or not self.stitch_enabled:
            output = self._generate_single(
                article,
                guidance=self._segment_guidance(
                    segments[0],
                    continuity,
                    visual_lock=visual_lock,
                    previous=None,
                    index=0,
                    total=1,
                ),
                seconds=int(segments[0]["seconds"]),
                ratio=ratio,
                size=size,
                progress=progress,
                output=self._output_path(article),
                progress_base=68,
                progress_span=12,
            )
            self._progress(progress, "video-downloading", 80)
            output = self._ensure_audio(
                output,
                article,
                target_seconds=target_seconds,
                progress=progress,
                segments=segments,
            )
            self._progress(progress, "video-ready", 82)
            return output

        self._progress(progress, "video-requesting", 68)
        clip_paths: list[Path] = []
        work_dir = self._segment_work_dir(article)
        try:
            previous_beat = ""
            for index, segment in enumerate(segments):
                seg_guidance = self._segment_guidance(
                    segment,
                    continuity,
                    visual_lock=visual_lock,
                    previous=previous_beat or None,
                    index=index,
                    total=len(segments),
                )
                clip_out = work_dir / f"seg-{index + 1:02d}-{segment['role']}.mp4"
                base = 68 + int(12 * index / max(1, len(segments)))
                span = max(2, int(12 / max(1, len(segments))))
                LOG.info(
                    "Generating video segment %s/%s role=%s seconds=%s topic=%s",
                    index + 1,
                    len(segments),
                    segment["role"],
                    segment["seconds"],
                    article.title[:40],
                )
                clip = self._generate_single(
                    article,
                    guidance=seg_guidance,
                    seconds=int(segment["seconds"]),
                    ratio=ratio,
                    size=size,
                    progress=progress,
                    output=clip_out,
                    progress_base=base,
                    progress_span=span,
                )
                clip_paths.append(clip)
                previous_beat = self._beat_memory(segment, visual_lock)

            self._progress(progress, "video-downloading", 80)
            final_path = self._output_path(article)
            self._stitch_clips(clip_paths, final_path)
            if not final_path.is_file() or final_path.stat().st_size < 1024:
                raise PublisherError("Stitched video file is empty or invalid")
            final_path = self._ensure_audio(
                final_path,
                article,
                target_seconds=target_seconds,
                progress=progress,
                segments=segments,
            )
            self._progress(progress, "video-ready", 82)
            return final_path
        finally:
            if not bool(self.video.get("keep_segments", False)):
                for path_item in clip_paths:
                    try:
                        if path_item.exists() and path_item.parent == work_dir:
                            path_item.unlink(missing_ok=True)  # type: ignore[arg-type]
                    except Exception:
                        pass
                try:
                    if work_dir.exists() and not any(work_dir.iterdir()):
                        work_dir.rmdir()
                except Exception:
                    pass

    @staticmethod
    def calc_segment_count(target_seconds: int, clip_max: int = 15, max_segments: int = 6) -> int:
        """User sets total duration; system auto-calc clip count by max 15s/clip."""
        target_seconds = max(2, int(target_seconds))
        clip_max = max(2, min(15, int(clip_max or 15)))
        max_segments = max(1, min(8, int(max_segments or 6)))
        if target_seconds <= clip_max:
            return 1
        return min(max_segments, max(2, math.ceil(target_seconds / clip_max)))

    def _plan_segments(self, target_seconds: int) -> list[dict[str, Any]]:
        """Auto split by user total duration: count = ceil(total / 15), each clip <= 15s."""
        clip_max = max(2, min(15, int(self.segment_max_seconds or 15)))
        target_seconds = max(2, int(target_seconds))
        count = self.calc_segment_count(target_seconds, clip_max, self.max_segments)
        if count == 1 or not self.stitch_enabled:
            return [
                {
                    "role": "single",
                    "seconds": min(target_seconds, clip_max),
                    "brief": "完整成片：开场建立主题，中段展开事实与画面，结尾自然收束。",
                }
            ]

        # distribute seconds as evenly as possible, each within [2, clip_max]
        base = max(2, min(clip_max, target_seconds // count))
        seconds_list = [base] * count
        remain = target_seconds - sum(seconds_list)
        i = 0
        while remain > 0 and i < count * clip_max:
            idx = i % count
            if seconds_list[idx] < clip_max:
                seconds_list[idx] += 1
                remain -= 1
            i += 1
        # if remain still >0 because of hard cap, keep at clip_max each (model limit)
        roles = self._segment_roles(count)
        briefs = self._segment_briefs(count)
        LOG.info(
            "Video plan: total=%ss clip_max=%ss segments=%s seconds=%s",
            target_seconds,
            clip_max,
            count,
            seconds_list,
        )
        return [
            {"role": roles[i], "seconds": int(seconds_list[i]), "brief": briefs[i]}
            for i in range(count)
        ]

    @staticmethod
    def _segment_roles(count: int) -> list[str]:
        if count == 2:
            return ["opening", "closing"]
        if count == 3:
            return ["opening", "development", "closing"]
        if count == 4:
            return ["opening", "development", "climax", "closing"]
        roles = ["opening"]
        mid = count - 2
        for i in range(mid):
            if i == mid - 1 and mid > 1:
                roles.append("climax")
            else:
                roles.append("development")
        roles.append("closing")
        return roles

    @staticmethod
    def _segment_briefs(count: int) -> list[str]:
        if count == 2:
            return [
                "开场动作：同一锁定人物进入同一锁定场景，建立站位与情绪，镜头缓慢推进。",
                "收尾动作：仍是同一人物与同一场景，动作自然收束并稳定定格，不要换装换景换风格。",
            ]
        if count == 3:
            return [
                "开场动作：锁定人物在锁定主场景出场，完成第一动作，确立光线与机位习惯。",
                "展开动作：同一人物在同一主场景（或紧邻连续空间）继续做事/互动，服装发型不变。",
                "收尾动作：同一人物同一场景收束情绪与动作，画面减速到稳定结束。",
            ]
        if count == 4:
            return [
                "开场动作：锁定人物进入主场景，建立关系与空间。",
                "展开动作：同一人物在主场景推进关键细节，道具与服装保持一致。",
                "推进动作：同一人物在连续空间完成关键转折动作，风格与光线不变。",
                "收尾动作：同一人物回到可识别主场景元素中收束，稳定结束。",
            ]
        briefs = ["开场动作：锁定人物与主场景建立。"]
        for i in range(1, count - 1):
            briefs.append(f"第{i + 1}段动作：同一人物、同一视觉风格、同一场景体系中推进情节。")
        briefs.append("收尾动作：同一人物与主场景元素收束，动作减速结束。")
        return briefs

    def _build_visual_lock(self, article: Article, guidance: str, ratio: str) -> dict[str, str]:
        """Freeze character / scene / style so all clips share one identity."""
        blob = " ".join(
            str(x)
            for x in (
                article.title,
                article.summary,
                article.topic,
                guidance,
                " ".join(article.tags or []),
                str(getattr(article, "body_markdown", "") or "")[:400],
            )
            if x
        )
        seed = abs(hash(blob)) % 10_000_000

        if any(k in blob for k in ("孩子", "儿童", "小学生", "幼儿")):
            cast = "一位约8-10岁东亚儿童，圆脸，短发，浅色外套，干净运动鞋"
        elif any(k in blob for k in ("老人", "退休", "大爷", "阿姨", "奶奶", "爷爷")):
            cast = "一位约60-70岁东亚长者，花白短发，深色休闲外套，温和面容"
        elif any(k in blob for k in ("球员", "足球", "篮球", "运动员", "世界杯", "比赛")):
            cast = "一位约25-30岁东亚青年运动员气质人物，利落短发，运动外套，专注神情"
        elif any(k in blob for k in ("医生", "护士", "医院")):
            cast = "一位约30-40岁东亚医护人员，整洁短发，浅色医护服，冷静表情"
        elif any(k in blob for k in ("老师", "课堂", "学校", "学生")):
            cast = "一位约28-35岁东亚教师形象，利落发型，简约衬衫或针织衫，亲和表情"
        elif any(k in blob for k in ("女性", "女生", "女")) and not any(k in blob for k in ("男性", "男生")):
            cast = "一位约25-32岁东亚女性，肩下黑发，素颜自然妆感，浅色风衣或针织衫"
        else:
            cast_pool = [
                "一位约28-35岁东亚男性，短黑发，深灰夹克，白T恤，干净运动鞋，平静表情",
                "一位约26-32岁东亚女性，黑长直发，米白针织衫，直筒裤，自然妆容",
                "一对东亚青年搭档（一男一女），穿着同色系便装，年龄约25-35岁",
            ]
            cast = cast_pool[seed % len(cast_pool)]

        if any(k in blob for k in ("球场", "足球", "世界杯", "赛场", "看台")):
            scene = "现代足球场看台与场边混合空间，绿色草坪与座椅清晰，比赛日人气但不杂乱"
            time_light = "傍晚金辉与场馆灯光混合，冷暖对比克制"
        elif any(k in blob for k in ("厨房", "做饭", "美食", "餐厅", "菜")):
            scene = "干净家用厨房与餐桌连续空间，木质台面，暖色家居道具"
            time_light = "白天窗光为主，柔和侧光"
        elif any(k in blob for k in ("医院", "诊室", "病房")):
            scene = "明亮医院走廊与诊室门口连续空间，简洁医疗环境"
            time_light = "冷白室内灯光，干净通透"
        elif any(k in blob for k in ("教室", "学校", "课堂")):
            scene = "明亮教室与走廊连续空间，课桌黑板清晰"
            time_light = "白天窗光，柔和均匀"
        elif any(k in blob for k in ("城市", "街头", "地铁", "写字楼", "公司", "办公室")):
            scene = "当代城市街道到室内入口的连续空间，玻璃幕墙与人行道"
            time_light = "清晨或黄昏自然光，城市冷灰调"
        elif any(k in blob for k in ("农村", "田野", "乡村", "田间")):
            scene = "中国乡村田野与院落连续空间，自然植被"
            time_light = "晴朗白天，柔和日光"
        else:
            scene_pool = [
                "当代中国城市生活场景：街边咖啡馆外到室内卡座的连续空间",
                "现代家居客厅与阳台连续空间，简洁北欧风家具",
                "开放式联合办公与落地窗城市景色连续空间",
            ]
            scene = scene_pool[seed % len(scene_pool)]
            time_light = "自然窗光，柔和对比，真实生活感"

        style_pool = [
            "纪实新闻纪录片风格，35mm镜头感，浅景深适中，手持微稳，胶片级色彩但不过度",
            "高端媒体短片风格，稳定云台运镜，真实光影，电影感但不炫技",
            "生活观察纪录片风格，自然机位，干净构图，低饱和真实色彩",
        ]
        style = style_pool[seed % len(style_pool)]
        palette_pool = [
            "青灰与暖肤色平衡，低饱和，统一调色LUT",
            "暖米与深棕为主，轻微青橙对比，全片同一调色",
            "冷蓝灰城市调，肤色自然，对比适中，全片一致",
        ]
        palette = palette_pool[seed % len(palette_pool)]
        camera = "同一套镜头语言：中近景与半身口型特写交替，人物口部清晰可辨，少量过肩与环境交代，运镜速度一致，避免跳轴"
        props = "固定可识别道具/符号贯穿全片（同一杯子/包/队服细节/门牌/桌面物件），帮助辨认同一个故事世界"

        return {
            "cast": cast,
            "scene": scene,
            "time_light": time_light,
            "style": style,
            "palette": palette,
            "camera": camera,
            "props": props,
            "ratio": ratio,
            "seed": str(seed),
        }

    def _format_visual_lock(self, lock: Mapping[str, str]) -> str:
        return (
            "【视觉锁定卡 | 全部分段必须逐字遵守，禁止改人设/换装/换场景风格】\n"
            f"- 人物锁定：{lock.get('cast', '')}\n"
            f"- 场景锁定：{lock.get('scene', '')}\n"
            f"- 光线时间：{lock.get('time_light', '')}\n"
            f"- 风格锁定：{lock.get('style', '')}\n"
            f"- 调色锁定：{lock.get('palette', '')}\n"
            f"- 镜头锁定：{lock.get('camera', '')}\n"
            f"- 贯穿道具：{lock.get('props', '')}\n"
            f"- 画幅锁定：{lock.get('ratio', '16:9')}\n"
            "- 硬约束：同一人物五官体态服装发型不变；主场景建筑/家具/天气不变；"
            "不得突然换城市、换季节、换滤镜、换片种；禁止文字/字幕/Logo/水印/UI；"
            "本段只改变“动作节拍”，不改变人设、场景世界观与美术风格。"
        )

    def _continuity_bible(
        self,
        article: Article,
        guidance: str,
        ratio: str,
        visual_lock: Mapping[str, str],
    ) -> str:
        tags = "、".join(article.tags[:4]) if article.tags else "无"
        lock_text = self._format_visual_lock(visual_lock)
        return (
            f"这是一条需多段拼接的连续短片，不是独立广告拼盘。\n"
            f"题材《{article.title}》；摘要：{article.summary}；"
            f"方向：{guidance or article.topic}；标签：{tags}。\n"
            f"{lock_text}\n"
            "叙事要求：分段=同一时间线的连续镜头；前后段动作、站位、朝向可衔接；"
            "开场建立人与空间，中段推进，收尾回到可识别元素。"
        )

    def _beat_memory(self, segment: Mapping[str, Any], visual_lock: Mapping[str, str]) -> str:
        return (
            f"角色仍是：{visual_lock.get('cast', '')}；"
            f"场景仍是：{visual_lock.get('scene', '')}；"
            f"刚完成节拍：{segment.get('brief') or segment.get('role')}；"
            f"风格仍是：{visual_lock.get('style', '')} / {visual_lock.get('palette', '')}"
        )

    def _segment_guidance(
        self,
        segment: Mapping[str, Any],
        continuity: str,
        *,
        visual_lock: Mapping[str, str],
        previous: str | None,
        index: int = 0,
        total: int = 1,
    ) -> str:
        role = str(segment.get("role") or "segment")
        brief = str(segment.get("brief") or "")
        lock_text = self._format_visual_lock(visual_lock)
        if previous:
            prev = (
                f"上一镜记忆（必须承接，不可重置世界观）：{previous}。"
                "请从可衔接的动作、朝向、机位继续；人物服装发型与场景陈设保持完全一致。"
            )
        else:
            prev = "这是成片第一镜：完整建立锁定人物与主场景，后续镜头都要认得出是同一人同一地。"
        role_line = {
            "opening": "本段职责=开场建立（定人、定景、定风格）。",
            "development": "本段职责=中段展开（只推进动作，不换人换景换风格）。",
            "climax": "本段职责=高潮推进（仍是同一人同一风格）。",
            "closing": "本段职责=收尾收束（回到同一人与可识别场景元素）。",
            "single": "本段职责=完整短片（内部也保持人景风格统一）。",
        }.get(role, f"本段职责={role}。")
        spoken = str(segment.get("line") or "").strip()
        if spoken:
            speak_block = (
                "【口型与台词硬约束 | lip-sync】\n"
                f"- 本段人物必须清晰口播这一句中文（不要改写、不要换语言）：「{spoken}」\n"
                "- 镜头以中近景为主，人物面部与口部清晰可见；说话时嘴唇随普通话音节自然开合，"
                "口型节奏与台词一致，不要闭嘴假说，不要无声对口型，不要旁白画外音式呆站。\n"
                "- 人物神态、手势与台词语义同步；整段都在说这一句，不要中途换另一段无关对白。"
            )
        else:
            speak_block = (
                "【口型约束】人物可自然微动口部表达，但不要无意义乱说话；"
                "保持面部清晰，便于后期配音贴合。"
            )
        return (
            f"{continuity}\n"
            f"{lock_text}\n"
            f"分段进度：{index + 1}/{total}。{role_line}\n"
            f"本段动作节拍：{brief}\n"
            f"{speak_block}\n"
            f"{prev}\n"
            "再次强调：人物一致、场景一致、风格一致；台词与口型一致；只拍这一段连续画面。"
        )

    def _segment_work_dir(self, article: Article) -> Path:
        output_dir = resolve_path(self.config_dir, self.video.get("output_dir", "./videos"))
        assert output_dir is not None
        work = output_dir / "_segments" / f"{utc_stamp()}-{slug(article.title)[:40]}"
        work.mkdir(parents=True, exist_ok=True)
        return work

    def _generate_single(
        self,
        article: Article,
        *,
        guidance: str,
        seconds: int,
        ratio: str,
        size: str,
        progress: ProgressCallback | None,
        output: Path,
        progress_base: int = 68,
        progress_span: int = 14,
    ) -> Path:
        payload = self._create_payload(article, guidance, seconds, ratio, size)
        # override prompt with segment-aware guidance already baked via _prompt using guidance
        self._progress(progress, "video-requesting", progress_base)
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        prefer_ipv4()
        with httpx_client(headers=headers, timeout=min(self.timeout, 120), follow_redirects=True) as client:
            response = client.post(self.base_url + self.create_path, json=payload)
            if not response.is_success and response.status_code in {400, 422}:
                # fallback: shorter prompt + safest resolution/duration
                LOG.warning(
                    "Video create %s, retry with simplified payload: %s",
                    response.status_code,
                    response.text[:240],
                )
                safe_seconds = self._normalize_duration(min(int(seconds), 8))
                safe_payload = {
                    "model": self.model,
                    "prompt": self._compact_prompt(
                        self._prompt(
                            article,
                            "Keep one consistent person, one consistent location, documentary style, no text overlays.",
                            safe_seconds,
                            ratio,
                        ),
                        limit=600,
                    ),
                    "duration": safe_seconds,
                    "resolution": "720p" if self._uses_grok_video_protocol() else payload.get("resolution", "720p"),
                }
                if self._uses_grok_video_protocol():
                    response = client.post(self.base_url + self.create_path, json=safe_payload)
                else:
                    response = client.post(self.base_url + self.create_path, json=payload)
            self._raise(response, "Video generation request failed")
            result = self._json(response)
            task_id = self._first(result, "id", "video_id", "task_id", "request_id")
            completed = self._is_complete(result)
            if not completed and not task_id:
                immediate = self._media_value(result)
                if immediate is None:
                    raise PublisherError("Video API returned neither a task id nor video content")
            deadline = time.monotonic() + self.timeout
            while not completed and task_id:
                if time.monotonic() >= deadline:
                    raise PublisherError(f"Video generation timed out after {int(self.timeout)} seconds")
                status = str(self._first(result, "status", "state") or "queued").lower()
                if status in {"failed", "error", "cancelled", "canceled", "rejected"}:
                    message = self._first(result, "error", "message", "detail") or status
                    raise PublisherError(f"Video generation failed: {message}")
                pct = progress_base + max(1, int(progress_span * 0.6))
                self._progress(progress, "video-generating", min(progress_base + progress_span - 1, pct))
                time.sleep(self.poll_interval)
                poll_path = self._poll_path(str(task_id))
                response = client.get(self.base_url + poll_path)
                self._raise(response, "Video generation status query failed")
                result = self._json(response)
                completed = self._is_complete(result)

            self._progress(progress, "video-downloading", min(81, progress_base + progress_span - 1))
            media = self._media_value(result)
            if isinstance(media, bytes):
                output.write_bytes(media)
            else:
                remote_url = media if isinstance(media, str) and media.startswith(("http://", "https://")) else None
                self._save_media(client, output, task_id=str(task_id) if task_id else None, remote_url=remote_url)
        if not output.is_file() or output.stat().st_size < 1024:
            raise PublisherError("Generated video file is empty or invalid")
        return output

    def _stitch_clips(self, clips: list[Path], output: Path) -> None:
        if not clips:
            raise PublisherError("No video clips to stitch")
        if len(clips) == 1:
            shutil.copyfile(clips[0], output)
            return
        ffmpeg = self.ffmpeg_bin
        if not shutil.which(ffmpeg) and not Path(ffmpeg).exists():
            raise PublisherError(
                "需要 ffmpeg 才能拼接多段视频，请在服务器安装 ffmpeg（或配置 video.ffmpeg 路径）"
            )

        # normalize clips to same fps/size; KEEP original audio (or add silence for uniform merge)
        work_dir = clips[0].parent
        normalized: list[Path] = []
        keep_audio = bool(self.video.get("stitch_keep_audio", True))
        for idx, clip in enumerate(clips):
            norm = work_dir / f"norm-{idx + 1:02d}.mp4"
            vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
            if keep_audio:
                if self._has_audio_stream(clip):
                    cmd = [
                        ffmpeg, "-y", "-i", str(clip),
                        "-vf", vf,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                        "-movflags", "+faststart",
                        str(norm),
                    ]
                else:
                    # no audio stream: inject silent track so later concat/acrossfade stays consistent
                    dur = max(0.5, self._probe_duration(clip))
                    cmd = [
                        ffmpeg, "-y",
                        "-i", str(clip),
                        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:duration={dur:.3f}",
                        "-vf", vf,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                        "-shortest",
                        "-movflags", "+faststart",
                        str(norm),
                    ]
            else:
                cmd = [
                    ffmpeg, "-y", "-i", str(clip),
                    "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-an",
                    str(norm),
                ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0 or not norm.is_file():
                err = (proc.stderr or proc.stdout or "")[-500:]
                raise PublisherError(f"ffmpeg normalize failed: {err}")
            normalized.append(norm)

        fade = self.stitch_crossfade
        if fade > 0 and len(normalized) >= 2:
            inputs: list[str] = []
            for npath in normalized:
                inputs.extend(["-i", str(npath)])
            durations = [self._probe_duration(p) for p in normalized]
            filter_parts: list[str] = []
            last_v = "[0:v]"
            last_a = "[0:a]"
            offset = max(0.0, durations[0] - fade)
            for i in range(1, len(normalized)):
                out_v = f"[v{i}]"
                out_a = f"[a{i}]"
                filter_parts.append(
                    f"{last_v}[{i}:v]xfade=transition=fade:duration={fade:.2f}:offset={offset:.2f}{out_v}"
                )
                if keep_audio:
                    filter_parts.append(
                        f"{last_a}[{i}:a]acrossfade=d={fade:.2f}:c1=tri:c2=tri{out_a}"
                    )
                last_v = out_v
                last_a = out_a
                if i < len(normalized) - 1:
                    offset += max(0.1, durations[i] - fade)
            filter_complex = ";".join(filter_parts)
            cmd = [
                ffmpeg, "-y", *inputs,
                "-filter_complex", filter_complex,
                "-map", last_v,
            ]
            if keep_audio:
                cmd += ["-map", last_a, "-c:a", "aac", "-b:a", "160k"]
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-movflags", "+faststart",
                str(output),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode == 0 and output.is_file() and output.stat().st_size >= 1024:
                LOG.info(
                    "Stitched %s clips with audio_keep=%s audio_stream=%s",
                    len(normalized),
                    keep_audio,
                    self._has_audio_stream(output),
                )
                self._cleanup_paths(normalized)
                return
            LOG.warning("xfade/acrossfade stitch failed, fallback to concat: %s", (proc.stderr or "")[-300:])

        # concat demuxer fallback (preserves audio when present on normalized clips)
        list_file = work_dir / "concat.txt"
        list_file.write_text(
            chr(10).join(f"file '{p.as_posix()}'" for p in normalized) + chr(10),
            encoding="utf-8",
        )
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        ]
        if keep_audio:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(output)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not output.is_file() or output.stat().st_size < 1024:
            err = (proc.stderr or proc.stdout or "")[-500:]
            raise PublisherError(f"ffmpeg concat failed: {err}")
        LOG.info(
            "Concat stitched %s clips audio_keep=%s audio_stream=%s",
            len(normalized),
            keep_audio,
            self._has_audio_stream(output),
        )
        self._cleanup_paths(normalized + [list_file])

    def _probe_duration(self, path: Path) -> float:
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return max(0.5, float((proc.stdout or "1").strip() or "1"))
        except Exception:
            return float(self.segment_max_seconds)

    @staticmethod
    def _cleanup_paths(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass

    def _has_audio_stream(self, path: Path) -> bool:
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return bool((proc.stdout or "").strip())
        except Exception:
            return False

    def _audio_mean_volume_db(self, path: Path) -> float | None:
        """Return mean volume in dB, or None if unavailable."""
        if not path.is_file():
            return None
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
        except Exception:
            return None
        err = proc.stderr or ""
        for line in err.splitlines():
            if "mean_volume:" in line:
                raw = line.split("mean_volume:", 1)[1].strip().split()[0]
                try:
                    return float(raw)
                except ValueError:
                    return None
        return None

    def _has_usable_audio(self, path: Path) -> bool:
        """True only when an audio stream exists and is not effectively silent."""
        if not self._has_audio_stream(path):
            return False
        mean = self._audio_mean_volume_db(path)
        if mean is None:
            # probe failed: treat as unusable so TTS still runs
            return False
        usable = mean > float(self.audio_silence_mean_db)
        LOG.info(
            "Audio loudness check %s mean=%.1f dB threshold=%.1f usable=%s",
            path.name,
            mean,
            float(self.audio_silence_mean_db),
            usable,
        )
        return usable


    def _split_sentences(self, text: str) -> list[str]:
        text = re.sub(r"\s+", " ", (text or "").strip())
        if not text:
            return []
        parts = re.split(r"(?<=[。！？.!?；;])\s*", text)
        out: list[str] = []
        for part in parts:
            part = part.strip().strip('"').strip("'").strip("“”")
            if not part:
                continue
            if not part.endswith(("。", "！", "？", ".", "!", "?", "；", ";")):
                part = part + "。"
            out.append(part)
        return out

    def _clip_text_budget(self, text: str, budget: int) -> str:
        text = re.sub(r"\s+", "", (text or "").strip())
        if len(text) <= budget:
            return text if (not text or text.endswith(("。", "！", "？", ".", "!", "?"))) else text + "。"
        cut = text[: max(1, budget - 1)].rstrip("，,、；;：:")
        return cut + "。"

    def _attach_narration_lines(
        self,
        article: Article,
        segments: list[dict[str, Any]],
        *,
        guidance: str = "",
    ) -> list[dict[str, Any]]:
        """Assign content-synced spoken lines for each segment (for lip-sync + TTS)."""
        title = str(article.title or "").strip()
        summary = str(article.summary or "").strip()
        body = str(getattr(article, "body_markdown", "") or "")
        body = re.sub(r"[#>*`\-]+", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        body_sents = [s for s in self._split_sentences(body) if s and s not in (title, summary)]
        if summary and summary not in title:
            body_sents = self._split_sentences(summary) + body_sents
        if guidance:
            body_sents.extend(self._split_sentences(str(guidance))[:2])

        result: list[dict[str, Any]] = []
        sent_idx = 0
        for _i, seg in enumerate(segments):
            sec = max(2, int(seg.get("seconds") or 8))
            budget = max(16, min(72, int((sec - 0.8) * 3.8)))
            role = str(seg.get("role") or "segment")
            if role in {"opening", "single"} and title:
                base = title if title.endswith(("。", "！", "？", ".", "!", "?")) else title + "。"
                if summary and summary not in base:
                    base = base + self._clip_text_budget(summary, max(12, budget - len(base)))
                line = self._clip_text_budget(base, budget)
            elif role == "closing":
                tail = body_sents[-1] if body_sents else f"关于{title or '这个热点'}，先记住关键结论。"
                if len(body_sents) >= 2:
                    tail = body_sents[-2] + body_sents[-1]
                line = self._clip_text_budget(tail, budget)
            else:
                chunk = ""
                while sent_idx < len(body_sents) and len(chunk) < budget:
                    chunk += body_sents[sent_idx]
                    sent_idx += 1
                if not chunk:
                    chunk = f"{title or '今日热点'}的关键信息继续展开。"
                line = self._clip_text_budget(chunk, budget)
            item = dict(seg)
            item["line"] = line
            result.append(item)
        return result

    def _fit_audio_duration(self, audio_path: Path, target_seconds: float, output_path: Path) -> Path:
        """Time-stretch/pad audio to match video segment duration."""
        target_seconds = max(0.8, float(target_seconds))
        src_dur = self._probe_duration(audio_path)
        if src_dur <= 0.1:
            shutil.copyfile(audio_path, output_path)
            return output_path

        ratio = src_dur / target_seconds
        filters: list[str] = []
        if ratio > 1.05:
            remain = ratio
            while remain > 2.0 + 1e-6:
                filters.append("atempo=2.0")
                remain /= 2.0
            filters.append(f"atempo={max(0.5, min(2.0, remain)):.4f}")
        elif ratio < 0.92:
            filters.append(f"atempo={max(0.85, min(1.0, ratio)):.4f}")
        filters.append("aresample=44100")
        filters.append(f"apad=whole_dur={target_seconds:.3f}")
        filters.append(f"atrim=0:{target_seconds:.3f}")
        af = ",".join(filters)
        cmd = [
            self.ffmpeg_bin, "-y",
            "-i", str(audio_path),
            "-af", af,
            "-c:a", "aac",
            "-b:a", "160k",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 200:
            return output_path
        cmd = [
            self.ffmpeg_bin, "-y",
            "-i", str(audio_path),
            "-af", f"apad,atrim=0:{target_seconds:.3f},aresample=44100",
            "-c:a", "aac",
            "-b:a", "160k",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and output_path.is_file():
            return output_path
        raise PublisherError(f"fit audio duration failed: {(proc.stderr or '')[-300:]}")

    def _build_timeline_voice(
        self,
        article: Article,
        *,
        target_seconds: int,
        work_dir: Path,
        segments: list[dict[str, Any]] | None = None,
    ) -> Path:
        """Build a full voice track whose chapters match segment durations/content."""
        work_dir.mkdir(parents=True, exist_ok=True)
        if not segments:
            script = self._narration_script(article, target_seconds=target_seconds)
            raw = work_dir / "voice-raw.mp3"
            fitted = work_dir / "voice-fitted.m4a"
            self._synthesize_tts(script, raw)
            return self._fit_audio_duration(raw, float(target_seconds), fitted)

        parts: list[Path] = []
        for i, seg in enumerate(segments):
            line = str(seg.get("line") or "").strip() or self._narration_script(
                article, target_seconds=int(seg.get("seconds") or 8)
            )
            sec = max(2.0, float(seg.get("seconds") or 8))
            raw = work_dir / f"seg-{i + 1:02d}-raw.mp3"
            fitted = work_dir / f"seg-{i + 1:02d}-fit.m4a"
            LOG.info("TTS segment %s/%s (%ss): %s", i + 1, len(segments), int(sec), line[:50])
            self._synthesize_tts(line, raw)
            self._fit_audio_duration(raw, sec, fitted)
            parts.append(fitted)

        if len(parts) == 1:
            final = work_dir / "voice-timeline.m4a"
            shutil.copyfile(parts[0], final)
            return final

        list_file = work_dir / "voice-concat.txt"
        list_file.write_text(chr(10).join(f"file '{p.as_posix()}'" for p in parts) + chr(10), encoding="utf-8")
        final = work_dir / "voice-timeline.m4a"
        cmd = [
            self.ffmpeg_bin, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c:a", "aac",
            "-b:a", "160k",
            str(final),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not final.is_file():
            raise PublisherError(f"concat timeline voice failed: {(proc.stderr or '')[-300:]}")
        total = max(float(target_seconds), sum(float(s.get("seconds") or 0) for s in segments))
        fitted_total = work_dir / "voice-timeline-fitted.m4a"
        return self._fit_audio_duration(final, total, fitted_total)

    def _narration_script(self, article: Article, *, target_seconds: int) -> str:
        budget = max(24, min(420, int(target_seconds * 3.6)))
        title = str(article.title or "").strip()
        summary = str(article.summary or "").strip()
        body = str(getattr(article, "body_markdown", "") or "").strip()
        body = re.sub(r"[#>*`\-]+", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        parts: list[str] = []
        if title:
            parts.append(title if title.endswith(("。", "！", "？", ".", "!", "?")) else title + "。")
        if summary and summary not in title:
            parts.append(summary if summary.endswith(("。", "！", "？", ".", "!", "?")) else summary + "。")
        if body:
            for sent in re.split(r"(?<=[。！？.!?])\s*", body):
                sent = sent.strip()
                if not sent or sent in title or sent in summary:
                    continue
                parts.append(sent if sent.endswith(("。", "！", "？", ".", "!", "?")) else sent + "。")
                if sum(len(p) for p in parts) >= budget:
                    break
        text = "".join(parts).strip() or f"关于{title or '今日热点'}的简要解读。"
        if len(text) > budget:
            text = text[: budget - 1].rstrip("，,、;； ") + "。"
        return text

    def _synthesize_tts(self, script: str, output_mp3: Path) -> Path:
        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        if output_mp3.exists():
            try:
                output_mp3.unlink()
            except Exception:
                pass
        text = (script or "").strip()
        if not text:
            raise PublisherError("TTS script is empty")

        # Prefer file input to avoid shell length / encoding issues with Chinese.
        text_file = output_mp3.with_suffix(".txt")
        text_file.write_text(text, encoding="utf-8")

        edge = shutil.which("edge-tts")
        py = sys.executable or shutil.which("python3") or shutil.which("python")
        candidates: list[list[str]] = []
        base_flags = [
            "--voice",
            self.tts_voice,
            "--rate",
            self.tts_rate,
            "--volume",
            self.tts_volume,
            "--file",
            str(text_file),
            "--write-media",
            str(output_mp3),
        ]
        if edge:
            candidates.append([edge, *base_flags])
        if py:
            candidates.append([py, "-m", "edge_tts", *base_flags])
            candidates.append(
                [
                    py,
                    "-m",
                    "edge_tts",
                    "--voice",
                    self.tts_voice,
                    "--rate",
                    self.tts_rate,
                    "--volume",
                    self.tts_volume,
                    "--text",
                    text,
                    "--write-media",
                    str(output_mp3),
                ]
            )

        last_err = ""
        for cmd in candidates:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode == 0 and output_mp3.is_file() and output_mp3.stat().st_size > 500:
                try:
                    text_file.unlink(missing_ok=True)
                except Exception:
                    pass
                return output_mp3
            last_err = (proc.stderr or proc.stdout or "")[-300:]

        # Python API fallback (more reliable under service environments)
        try:
            import asyncio
            import edge_tts  # type: ignore

            async def _run() -> None:
                communicate = edge_tts.Communicate(
                    text,
                    self.tts_voice,
                    rate=self.tts_rate,
                    volume=self.tts_volume,
                )
                await communicate.save(str(output_mp3))

            asyncio.run(_run())
            if output_mp3.is_file() and output_mp3.stat().st_size > 500:
                try:
                    text_file.unlink(missing_ok=True)
                except Exception:
                    pass
                return output_mp3
        except Exception as exc:  # noqa: BLE001
            last_err = f"{last_err} | edge_tts api: {exc}"

        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak:
            wav = output_mp3.with_suffix(".wav")
            proc = subprocess.run(
                [espeak, "-v", "zh", "-s", "150", "-w", str(wav), text],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and wav.is_file():
                out = output_mp3.with_suffix(".m4a")
                conv = subprocess.run(
                    [self.ffmpeg_bin, "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(out)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if conv.returncode == 0 and out.is_file():
                    try:
                        wav.unlink(missing_ok=True)
                        text_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return out
        try:
            text_file.unlink(missing_ok=True)
        except Exception:
            pass
        raise PublisherError(f"TTS synthesis failed: {last_err or 'no TTS engine'}")

    def _make_soft_bed(self, duration: float, output_path: Path) -> Path | None:
        ffmpeg = self.ffmpeg_bin
        if not shutil.which(ffmpeg) and not Path(ffmpeg).exists():
            return None
        duration = max(1.0, float(duration))
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=pink:amplitude=0.015:duration={duration:.2f}",
            "-af",
            "lowpass=f=600,volume=0.25",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 200:
            return output_path
        return None

    def _mux_audio(self, video_path: Path, voice_path: Path, output_path: Path, *, bed_path: Path | None = None) -> Path:
        ffmpeg = self.ffmpeg_bin
        video_dur = self._probe_duration(video_path)
        if bed_path and bed_path.is_file():
            filter_complex = (
                f"[1:a]aresample=44100,apad,atrim=0:{video_dur:.2f},volume=1.0[voice];"
                f"[2:a]aresample=44100,apad,atrim=0:{video_dur:.2f},volume={self.bgm_volume:.2f}[bed];"
                f"[voice][bed]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )
            cmd = [
                ffmpeg, "-y",
                "-i", str(video_path),
                "-i", str(voice_path),
                "-i", str(bed_path),
                "-filter_complex", filter_complex,
                "-map", "0:v:0",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "160k",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
        else:
            filter_complex = f"[1:a]aresample=44100,apad,atrim=0:{video_dur:.2f}[aout]"
            cmd = [
                ffmpeg, "-y",
                "-i", str(video_path),
                "-i", str(voice_path),
                "-filter_complex", filter_complex,
                "-map", "0:v:0",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "160k",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not output_path.is_file() or output_path.stat().st_size < 1024:
            err = (proc.stderr or proc.stdout or "")[-500:]
            raise PublisherError(f"ffmpeg audio mux failed: {err}")
        return output_path

    def _ensure_audio(
        self,
        video_path: Path,
        article: Article,
        *,
        target_seconds: int,
        progress: ProgressCallback | None = None,
        segments: list[dict[str, Any]] | None = None,
    ) -> Path:
        if not self.audio_enabled:
            return video_path

        has_stream = self._has_audio_stream(video_path)
        usable = self._has_usable_audio(video_path) if has_stream else False
        # Keep real original audio from source clips / stitched result.
        # Only synthesize TTS when audio is missing or effectively silent.
        if usable:
            LOG.info("Keep original usable audio (no TTS replace): %s", video_path.name)
            return video_path
        if has_stream and not usable:
            LOG.info("Video has silent/placeholder audio track, will replace with TTS: %s", video_path.name)
        else:
            LOG.info("Video has no audio stream, will synthesize TTS: %s", video_path.name)

        self._progress(progress, "video-downloading", 81)
        work_dir = video_path.parent / "_audio"
        work_dir.mkdir(parents=True, exist_ok=True)
        stem = video_path.stem
        video_dur = self._probe_duration(video_path)
        voice_path: Path | None = None
        try:
            LOG.info(
                "Synthesizing content-synced timeline TTS for %s (segments=%s, target=%ss, video=%.1fs)",
                video_path.name,
                len(segments or []),
                target_seconds,
                video_dur,
            )
            voice_path = self._build_timeline_voice(
                article,
                target_seconds=max(target_seconds, int(round(video_dur))),
                work_dir=work_dir / f"{stem}-timeline",
                segments=segments,
            )
            fitted = work_dir / f"{stem}-voice-final.m4a"
            voice_path = self._fit_audio_duration(Path(voice_path), video_dur, fitted)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Timeline TTS failed (%s), fallback flat script", exc)
            try:
                script = self._narration_script(article, target_seconds=target_seconds)
                raw = work_dir / f"{stem}-voice.mp3"
                fitted = work_dir / f"{stem}-voice-final.m4a"
                self._synthesize_tts(script, raw)
                voice_path = self._fit_audio_duration(raw, video_dur, fitted)
            except Exception as exc2:  # noqa: BLE001
                LOG.warning("TTS failed (%s), try ambient-only bed", exc2)
                voice_path = None

        bed_path = work_dir / f"{stem}-bed.m4a"
        bed = self._make_soft_bed(video_dur, bed_path)
        out_path = video_path.with_name(video_path.stem + "-voiced.mp4")
        try:
            if voice_path and Path(voice_path).is_file():
                self._mux_audio(video_path, Path(voice_path), out_path, bed_path=bed)
            elif bed:
                cmd = [
                    self.ffmpeg_bin, "-y",
                    "-i", str(video_path),
                    "-i", str(bed),
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-shortest",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if proc.returncode != 0:
                    LOG.warning("ambient mux failed: %s", (proc.stderr or "")[-200:])
                    return video_path
            else:
                return video_path

            if out_path.is_file() and out_path.stat().st_size > 1024:
                if not self._has_audio_stream(out_path):
                    LOG.warning("Muxed file missing audio stream: %s", out_path.name)
                    return video_path
                try:
                    video_path.unlink(missing_ok=True)
                except Exception:
                    pass
                out_path.replace(video_path)
                LOG.info("Content-synced audio attached: %s", video_path.name)
                return video_path
        finally:
            if not bool(self.video.get("keep_audio_temp", False)):
                for p in work_dir.glob(f"{stem}-*"):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    if work_dir.exists() and not any(work_dir.iterdir()):
                        work_dir.rmdir()
                except Exception:
                    pass
        return video_path


    def _save_media(
        self,
        client: httpx.Client,
        output: Path,
        *,
        task_id: str | None,
        remote_url: str | None,
    ) -> None:
        errors: list[str] = []

        if task_id and self.download_url_template:
            try:
                url = self.download_url_template.format(
                    id=task_id,
                    request_id=task_id,
                    base_url=self.base_url,
                    url=remote_url or "",
                )
                if not url.startswith(("http://", "https://")):
                    url = self.base_url.rstrip("/") + "/" + url.lstrip("/")
                self._download_url(client, url, output, label="template")
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"template: {exc}")
                LOG.warning("Template download failed: %s", exc)

        if remote_url:
            # China VMs usually cannot reach xAI CDN directly; prefer proxy/curl first.
            if self.http_proxy:
                try:
                    self._download_via_curl(remote_url, output, proxy=self.http_proxy)
                    if output.is_file() and output.stat().st_size >= 1024:
                        return
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"curl-proxy: {exc}")
                    LOG.warning("Proxy curl download failed (%s)", exc)
            try:
                self._download_url(
                    client,
                    remote_url,
                    output,
                    label="direct",
                    allow_proxy=True,
                    short_connect=True,
                    force_proxy=bool(self.http_proxy),
                )
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"direct: {exc}")
                LOG.warning("Direct video URL download failed (%s); trying gateway endpoints", exc)

        if task_id:
            for label, path in self._gateway_download_candidates(task_id, remote_url):
                try:
                    url = path if path.startswith(("http://", "https://")) else self.base_url.rstrip("/") + path
                    self._download_url(client, url, output, label=label, allow_proxy=False)
                    return
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{label}: {exc}")
                    LOG.warning("Gateway download candidate failed (%s): %s", label, exc)

        if remote_url and self.http_proxy:
            try:
                self._download_url(
                    client,
                    remote_url,
                    output,
                    label="proxy-direct",
                    allow_proxy=True,
                    force_proxy=True,
                )
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"proxy-direct: {exc}")

        detail = " | ".join(errors[:8]) if errors else "no download candidates"
        proxy_hint = (
            f"已配置代理 {self.http_proxy} 但仍失败"
            if self.http_proxy
            else "当前未配置 video.http_proxy / HTTPS_PROXY，国内机器常无法直连 xAI CDN(vidgen.x.ai)"
        )
        raise PublisherError(
            "视频已生成，但下载失败。"
            f"任务ID={task_id or '-'}；远程URL={remote_url or '-'}；{proxy_hint}。"
            "可在 [video] 配置 http_proxy，或改用可从国内下载的视频模型/中转。"
            f" 细节: {detail}"
        )

    def _gateway_download_candidates(self, task_id: str, remote_url: str | None) -> list[tuple[str, str]]:
        content_path = self._content_path(task_id)
        encoded = quote(remote_url or "", safe="")
        candidates: list[tuple[str, str] | None] = [
            ("content_path", content_path),
            ("download_id", f"/videos/download?id={task_id}"),
            ("download_request_id", f"/videos/download?request_id={task_id}"),
            ("download_url", f"/videos/download?url={encoded}") if remote_url else None,
            ("videos_content_query", f"/videos/content?id={task_id}"),
            ("videos_id_download", f"/videos/{task_id}/download"),
            ("videos_id_file", f"/videos/{task_id}/file"),
            ("videos_id_mp4", f"/videos/{task_id}/mp4"),
            ("files_content", f"/files/{task_id}/content"),
            ("generations_content", f"/videos/generations/{task_id}/content"),
        ]
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in candidates:
            if not item:
                continue
            label, path = item
            if not path.startswith("http"):
                path = "/" + path.lstrip("/")
            if path in seen:
                continue
            seen.add(path)
            out.append((label, path))
        return out

    def _download_url(
        self,
        client: httpx.Client,
        url: str,
        output: Path,
        *,
        label: str,
        allow_proxy: bool = True,
        force_proxy: bool = False,
        short_connect: bool = False,
    ) -> None:
        headers = dict(client.headers)
        timeout = httpx.Timeout(
            self.direct_timeout if short_connect else min(self.timeout, 180),
            connect=self.direct_connect_timeout if short_connect else 15.0,
        )
        proxies = self.http_proxy if (allow_proxy and self.http_proxy) else None
        if force_proxy and not proxies:
            raise PublisherError("proxy forced but not configured")

        last_error: Exception | None = None
        attempts: list[tuple[str, dict[str, Any]]] = []
        if not force_proxy:
            attempts.append(("ipv4", {}))
        if proxies:
            attempts.append(("proxy", {"proxy": proxies}))

        for attempt_label, kwargs in attempts:
            try:
                with httpx_client(
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=True,
                    **kwargs,
                ) as dl_client:
                    with dl_client.stream("GET", url) as response:
                        self._raise(response, f"Video URL download failed[{label}/{attempt_label}]")
                        content_type = response.headers.get("content-type", "")
                        if "application/json" in content_type and "video" not in content_type:
                            body = b"".join(response.iter_bytes())
                            try:
                                payload = json.loads(body.decode("utf-8", errors="replace"))
                            except json.JSONDecodeError as exc:
                                raise PublisherError(
                                    f"Video download returned JSON decode error: {body[:200]!r}"
                                ) from exc
                            nested = self._media_value(payload)
                            if isinstance(nested, bytes):
                                output.write_bytes(nested)
                                return
                            if isinstance(nested, str) and nested.startswith(("http://", "https://")):
                                if nested.rstrip("/") == url.rstrip("/"):
                                    raise PublisherError("Video download JSON nested url loops")
                                self._download_url(
                                    client,
                                    nested,
                                    output,
                                    label=f"{label}-nested",
                                    allow_proxy=True,
                                    short_connect=True,
                                )
                                return
                            raise PublisherError(f"Video download JSON without media: {body[:300]!r}")
                        self._write_stream(response, output)
                        if output.is_file() and output.stat().st_size >= 1024:
                            return
                        raise PublisherError("Downloaded video too small")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                LOG.warning("download %s/%s failed: %s", label, attempt_label, exc)
        # curl fallback (better proxy/TLS compatibility on some China hosts)
        try:
            self._download_via_curl(url, output, proxy=proxies if allow_proxy else None)
            if output.is_file() and output.stat().st_size >= 1024:
                return
        except Exception as curl_exc:  # noqa: BLE001
            last_error = curl_exc
            LOG.warning("download %s/curl failed: %s", label, curl_exc)
        raise PublisherError(f"{label} download failed: {last_error}")


    def _download_via_curl(self, url: str, output: Path, *, proxy: str | None = None) -> None:
        import subprocess

        cmd = [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "2",
            "--connect-timeout",
            str(int(self.direct_connect_timeout)),
            "--max-time",
            str(int(max(self.direct_timeout, 120))),
            "-A",
            "Mozilla/5.0",
            "-o",
            str(output),
        ]
        if proxy:
            cmd.extend(["-x", proxy])
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise PublisherError("curl not available for video download fallback") from exc
        if proc.returncode != 0 or not output.is_file() or output.stat().st_size < 1024:
            err = (proc.stderr or proc.stdout or "")[:400]
            raise PublisherError(f"curl download failed code={proc.returncode}: {err}")

    def _output_path(self, article: Article) -> Path:
        output_dir = resolve_path(self.config_dir, self.video.get("output_dir", "./videos"))
        assert output_dir is not None
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_stamp()
        name = slug(article.title)
        return output_dir / f"{stamp}-{name}.mp4"

    def _poll_path(self, task_id: str) -> str:
        default = "/videos/{id}" if self._uses_grok_video_protocol() else f"{self.create_path}/{{id}}"
        template = str(self.video.get("poll_path") or default)
        return "/" + template.format(id=task_id).strip("/")

    def _content_path(self, task_id: str) -> str:
        if self._uses_grok_video_protocol():
            default = "/videos/{id}/content"
        else:
            default = f"{self.create_path}/{{id}}/content"
        template = str(self.video.get("content_path") or default)
        return "/" + template.format(id=task_id).strip("/")

    def _size_for_ratio(self, ratio: str) -> str:
        configured = str(self.video.get("size") or "").strip()
        default_ratio = str(self.video.get("aspect_ratio") or "16:9")
        if configured and ratio == default_ratio:
            return configured
        return {"9:16": "720x1280", "1:1": "1024x1024"}.get(ratio, "1280x720")

    def _create_payload(
        self,
        article: Article,
        guidance: str,
        seconds: int,
        ratio: str,
        size: str,
    ) -> dict[str, Any]:
        seconds = self._normalize_duration(seconds)
        prompt = self._compact_prompt(self._prompt(article, guidance, seconds, ratio))
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
        }
        if self._uses_grok_video_protocol():
            payload.update(
                {
                    "duration": int(seconds),
                    "resolution": self._resolution_for_size(size),
                }
            )
        else:
            payload.update(
                {
                    str(self.video.get("duration_field") or "seconds"): str(seconds),
                    "size": size,
                }
            )
        return payload

    def _uses_grok_video_protocol(self) -> bool:
        model = self.model.lower()
        return (
            self.create_path.rstrip("/") == "/videos/generations"
            or "grok" in model
            or "imagine-video" in model
        )

    def _resolution_for_size(self, size: str) -> str:
        # grok-imagine-video via mid-gateway rejects 1080p (HTTP 400)
        configured = str(self.video.get("resolution") or "").strip().lower()
        if configured in {"480p", "720p"}:
            return configured
        if configured in {"1080p", "1080", "fullhd"}:
            return "720p"
        try:
            width, height = (int(part) for part in size.lower().split("x", 1))
            short_edge = min(width, height)
        except (TypeError, ValueError):
            return "720p"
        if short_edge >= 720:
            return "720p"
        return "480p"

    def _normalize_duration(self, seconds: int) -> int:
        """Clamp to model-supported clip lengths."""
        allowed = [4, 5, 6, 8, 10, 12, 15]
        segment_max = int(getattr(self, "segment_max_seconds", 15) or 15)
        seconds = max(2, min(int(seconds), segment_max, 15))
        if seconds in allowed:
            return seconds
        # prefer nearest allowed; ties -> longer for smoother stitch coverage
        return min(allowed, key=lambda d: (abs(d - seconds), -d))

    def _compact_prompt(self, prompt: str, *, limit: int = 1200) -> str:
        prompt = " ".join(str(prompt or "").split())
        if len(prompt) <= limit:
            return prompt
        return prompt[: limit - 20].rstrip() + " ..."

    @staticmethod
    def _prompt(article: Article, guidance: str, duration: int, ratio: str) -> str:
        return (
            f"Create a {duration}-second {ratio} editorial news video for Chinese viewers. "
            f"Topic: {article.title}. Summary: {article.summary}. "
            f"Direction and hard visual locks: {guidance or article.topic}. "
            "Keep ONE consistent cast, ONE consistent main location/world, and ONE consistent cinematic style "
            "throughout the whole clip. Do not change faces, outfits, hairstyles, age, weather, color grade, "
            "or art direction. Continuous storytelling for multi-clip stitching. "
            "If a spoken Chinese line is specified, the same character must lip-sync that exact line: "
            "clear facial close/medium shot, natural Mandarin mouth shapes matching each syllable, "
            "gesture and expression synchronized with the line. "
            "Concrete documentary scenes, natural motion, stable camera, accurate details, "
            "no embedded captions, logos, or watermarks."
        )

    @staticmethod
    def _first(payload: Any, *keys: str) -> Any:
        queue = [payload]
        while queue:
            item = queue.pop(0)
            if isinstance(item, Mapping):
                for key in keys:
                    value = item.get(key)
                    if value not in (None, ""):
                        return value
                for key in ("data", "result", "output", "video"):
                    value = item.get(key)
                    if isinstance(value, (Mapping, list)):
                        queue.append(value)
            elif isinstance(item, list):
                queue.extend(item[:5])
        return None

    def _media_value(self, payload: Any) -> str | bytes | None:
        b64_value = self._first(payload, "b64_json", "base64", "video_base64")
        if isinstance(b64_value, str) and b64_value:
            try:
                return base64.b64decode(b64_value)
            except ValueError as exc:
                raise PublisherError("Video API returned invalid base64 content") from exc
        value = self._first(payload, "url", "download_url", "video_url", "content_url")
        return str(value) if value else None

    @staticmethod
    def _is_complete(payload: Any) -> bool:
        status = str(VideoGenerator._first(payload, "status", "state") or "").lower()
        return status in {"completed", "succeeded", "success", "done", "finished"}

    @staticmethod
    def _status_percent(payload: Any) -> int:
        raw = VideoGenerator._first(payload, "progress", "percent", "percentage")
        try:
            value = float(raw)
            if value <= 1:
                value *= 100
            return max(69, min(78, 69 + int(value * 0.09)))
        except (TypeError, ValueError):
            return 72

    def _write_stream(self, response: httpx.Response, output: Path) -> None:
        total = 0
        with output.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > self.max_download_bytes:
                    raise PublisherError("Generated video exceeds the configured download limit")
                handle.write(chunk)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PublisherError("Video API returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise PublisherError("Video API response JSON must be an object")
        return payload

    @staticmethod
    def _raise(response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        detail = ""
        try:
            # stream responses may not expose .text until read
            if not getattr(response, "is_stream_consumed", True):
                raw = response.read()
                detail = raw[:500].decode("utf-8", errors="replace")
            else:
                detail = (response.text or "")[:500]
        except Exception:
            detail = f"status={response.status_code}"
        raise PublisherError(f"{message} (HTTP {response.status_code}): {detail}")

    @staticmethod
    def _progress(callback: ProgressCallback | None, stage: str, percent: int) -> None:
        if callback:
            callback(stage, percent)
