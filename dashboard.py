#!/usr/bin/env python3
"""Local web dashboard for hot-topic generation and Toutiao publishing."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app_auth import SessionSigner, UserStore
from db import init_db, load_or_migrate, migrate_state_tree, save_store
from model_profiles import ModelProfileStore
from hot_topics import HotTopicService
from net_utils import prefer_ipv4

prefer_ipv4()
from toutiao_accounts import AccountStore, QRLoginManager, storage_paths
from toutiao_challenges import (
    ToutiaoChallengeClient,
    activity_reward_value,
    score_activity_for_topic,
)
from toutiao_protocol import (
    LoginRequired,
    ProtocolChallenge,
    PublisherError,
    ToutiaoProtocolClient,
    credential_summary,
)
from toutiao_publisher import (
    Article,
    ArticleGenerator,
    CoverGenerator,
    load_toml,
    resolve_path,
    save_article,
)
from toutiao_video import ToutiaoVideoClient
from video_generation import VideoGenerator


LOG = logging.getLogger("toutiao-dashboard")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def challenge_acceptance_is_current(
    record: Mapping[str, Any], repeat_mode: str, today: Any
) -> bool:
    if repeat_mode == "daily":
        return str(record.get("accepted_on") or "") == today.isoformat()
    if repeat_mode == "weekly":
        try:
            accepted_date = datetime.fromisoformat(str(record.get("accepted_on"))).date()
        except (TypeError, ValueError):
            return False
        return accepted_date.isocalendar()[:2] == today.isocalendar()[:2]
    return True


class StateStore:
    def __init__(self, path: Path, automation_defaults: dict[str, Any]) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        default_automation = {
            "enabled": bool(automation_defaults.get("enabled", False)),
            "interval_minutes": int(automation_defaults.get("interval_minutes", 60)),
            "mode": str(automation_defaults.get("mode", "draft")),
            "content_type": str(automation_defaults.get("content_type", "article") or "article"),
            "video_duration": int(automation_defaults.get("video_duration", 15) or 15),
            "auto_claim_challenges": bool(automation_defaults.get("auto_claim_challenges", True)),
            "pick_count": int(automation_defaults.get("pick_count", 1)),
            "categories": list(automation_defaults.get("categories", [])),
            "sources": list(automation_defaults.get("sources", [])),
            "last_run": None,
            "next_run": None,
        }
        if default_automation["content_type"] not in {"article", "video"}:
            default_automation["content_type"] = "article"
        default_automation["video_duration"] = max(2, min(60, int(default_automation["video_duration"])))
        self.data: dict[str, Any] = {
            "jobs": [],
            "challenge_acceptances": {},
            "automation": {
                "defaults": default_automation,
                "accounts": {},
                "legacy": None,
            },
            "session": {"status": "idle", "message": "", "updated_at": now_iso()},
        }
        try:
            saved = load_or_migrate(self.path, kind="dashboard")
            if isinstance(saved, dict):
                self.data["jobs"] = saved.get("jobs", [])
                saved_acceptances = saved.get("challenge_acceptances", {})
                if isinstance(saved_acceptances, dict):
                    self.data["challenge_acceptances"] = saved_acceptances
                saved_automation = saved.get("automation", {})
                if isinstance(saved_automation, dict):
                    if isinstance(saved_automation.get("accounts"), dict):
                        self.data["automation"]["accounts"] = saved_automation["accounts"]
                        if isinstance(saved_automation.get("defaults"), dict):
                            self.data["automation"]["defaults"].update(
                                saved_automation["defaults"]
                            )
                    else:
                        legacy = copy.deepcopy(default_automation)
                        legacy.update(saved_automation)
                        self.data["automation"]["legacy"] = legacy
                self.data["session"].update(saved.get("session", saved.get("login", {})))
        except Exception:
            LOG.exception("Could not load dashboard state")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data)

    def add_job(self, job: dict[str, Any]) -> None:
        with self.lock:
            self.data["jobs"].insert(0, job)
            self.data["jobs"] = self.data["jobs"][:200]
            self._save()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = next((item for item in self.data["jobs"] if item["id"] == job_id), None)
            return copy.deepcopy(job) if job else None

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            job = next((item for item in self.data["jobs"] if item["id"] == job_id), None)
            if job is None:
                raise KeyError(job_id)
            job.update(changes)
            job["updated_at"] = now_iso()
            self._save()
            return copy.deepcopy(job)

    def delete_job(self, job_id: str) -> bool:
        with self.lock:
            before = len(self.data["jobs"])
            self.data["jobs"] = [item for item in self.data["jobs"] if item["id"] != job_id]
            changed = len(self.data["jobs"]) != before
            if changed:
                self._save()
            return changed

    def bind_unassigned_jobs(self, account_id: str, account_name: str) -> int:
        if not account_id:
            return 0
        with self.lock:
            changed = 0
            for job in self.data["jobs"]:
                if job.get("account_id"):
                    continue
                job["account_id"] = account_id
                job["account_name"] = account_name
                changed += 1
            if changed:
                self._save()
            return changed

    def accepted_challenge_ids(
        self,
        account_id: str,
        activities: list[dict[str, Any]] | None = None,
    ) -> set[str]:
        with self.lock:
            account_records = self.data.get("challenge_acceptances", {}).get(account_id, {})
            if not isinstance(account_records, dict):
                return set()
            today = datetime.now().astimezone().date()
            current_modes = {
                str(activity.get("id") or activity.get("activity_id") or ""): str(
                    activity.get("repeat_mode") or "unknown"
                )
                for activity in (activities or [])
                if isinstance(activity, dict)
                and str(activity.get("repeat_mode") or "unknown")
                in {"daily", "weekly", "once"}
            }
            return {
                str(activity_id)
                for activity_id, record in account_records.items()
                if isinstance(record, dict)
                and challenge_acceptance_is_current(
                    record,
                    current_modes.get(
                        str(activity_id), str(record.get("repeat_mode") or "once")
                    ),
                    today,
                )
            }

    def challenge_acceptance_records(self, account_id: str) -> dict[str, dict[str, Any]]:
        with self.lock:
            records = self.data.get("challenge_acceptances", {}).get(account_id, {})
            return copy.deepcopy(records) if isinstance(records, dict) else {}

    def update_challenge_metadata(
        self,
        account_id: str,
        activity_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        repeat_mode = str(metadata.get("repeat_mode") or "unknown")
        if repeat_mode not in {"daily", "weekly", "once"}:
            return
        with self.lock:
            account_records = self.data.get("challenge_acceptances", {}).get(account_id, {})
            record = account_records.get(str(activity_id)) if isinstance(account_records, dict) else None
            if not isinstance(record, dict):
                return
            record.update(
                {
                    "repeat_mode": repeat_mode,
                    "repeat_reason": str(metadata.get("repeat_reason") or ""),
                    "daily_repeatable": repeat_mode == "daily",
                    "weekly_repeatable": repeat_mode == "weekly",
                    "detail_url": str(metadata.get("detail_url") or record.get("detail_url") or ""),
                    "updated_at": now_iso(),
                }
            )
            self._save()

    def accept_challenges(
        self,
        account_id: str,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not account_id:
            raise ValueError("头条账号 ID 为空")
        with self.lock:
            acceptances = self.data.setdefault("challenge_acceptances", {})
            account_records = acceptances.setdefault(account_id, {})
            now = now_iso()
            today = datetime.now().astimezone().date()
            records: list[dict[str, Any]] = []
            new_count = 0
            for activity in activities:
                activity_id = str(activity.get("id") or activity.get("activity_id") or "").strip()
                if not activity_id.isdigit():
                    continue
                previous = account_records.get(activity_id, {})
                if not isinstance(previous, dict):
                    previous = {}
                incoming_repeat_mode = str(activity.get("repeat_mode") or "unknown")
                if incoming_repeat_mode in {"daily", "weekly", "once"}:
                    repeat_mode = incoming_repeat_mode
                elif str(previous.get("repeat_mode") or "") in {"daily", "weekly", "once"}:
                    repeat_mode = str(previous["repeat_mode"])
                else:
                    repeat_mode = "once"
                if incoming_repeat_mode in {"daily", "weekly", "once"}:
                    repeat_reason = str(
                        activity.get("repeat_reason")
                        or previous.get("repeat_reason")
                        or "未发现每日重复规则"
                    )
                else:
                    repeat_reason = str(
                        previous.get("repeat_reason")
                        or activity.get("repeat_reason")
                        or "未发现每日重复规则"
                    )
                already_accepted = bool(previous) and challenge_acceptance_is_current(
                    previous,
                    repeat_mode,
                    today,
                )
                if not already_accepted:
                    new_count += 1
                record = {
                    "activity_id": activity_id,
                    "account_id": account_id,
                    "title": str(activity.get("title") or previous.get("title") or "创作活动"),
                    "introduction": str(
                        activity.get("introduction") or previous.get("introduction") or ""
                    ),
                    "reward_label": str(
                        activity.get("reward_label") or previous.get("reward_label") or ""
                    ),
                    "max_award": int(activity.get("max_award") or previous.get("max_award") or 0),
                    "forum_id": str(activity.get("forum_id") or previous.get("forum_id") or ""),
                    "activity_time": str(
                        activity.get("activity_time") or previous.get("activity_time") or ""
                    ),
                    "content_type": str(
                        activity.get("content_type") or previous.get("content_type") or "article"
                    ),
                    "biz_id": int(activity.get("biz_id") or previous.get("biz_id") or 1),
                    "activity_time": str(
                        activity.get("activity_time") or previous.get("activity_time") or ""
                    ),
                    "detail_url": str(
                        activity.get("detail_url") or previous.get("detail_url") or ""
                    ),
                    "repeat_mode": repeat_mode,
                    "repeat_reason": repeat_reason,
                    "daily_repeatable": repeat_mode == "daily",
                    "weekly_repeatable": repeat_mode == "weekly",
                    "accepted_at": str(previous.get("accepted_at") or now),
                    "accepted_on": today.isoformat(),
                    "last_accepted_at": now,
                    "claim_count": int(previous.get("claim_count") or 0)
                    + (0 if already_accepted else 1),
                    "updated_at": now,
                }
                account_records[activity_id] = record
                records.append(copy.deepcopy(record))
            if len(account_records) > 10000:
                ordered = sorted(
                    account_records.values(),
                    key=lambda item: str(item.get("updated_at") or ""),
                    reverse=True,
                )[:10000]
                acceptances[account_id] = {
                    str(item["activity_id"]): item for item in ordered
                }
            if records:
                self._save()
            return {
                "records": records,
                "accepted_count": len(records),
                "new_count": new_count,
                "existing_count": len(records) - new_count,
            }

    def update_automation_account(self, account_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            accounts = self.data["automation"].setdefault("accounts", {})
            profile = copy.deepcopy(
                accounts.get(account_id) or self.data["automation"].get("defaults", {})
            )
            profile.update(changes)
            accounts[account_id] = profile
            self.data["automation"]["legacy"] = None
            self._save()
            return copy.deepcopy(profile)

    def replace_automation_accounts(self, accounts: dict[str, dict[str, Any]]) -> None:
        with self.lock:
            self.data["automation"]["accounts"] = copy.deepcopy(accounts)
            self.data["automation"]["legacy"] = None
            self._save()

    def update_session(self, status: str, message: str = "") -> None:
        with self.lock:
            self.data["session"] = {"status": status, "message": message, "updated_at": now_iso()}
            self._save()

    def _save(self) -> None:
        save_store(self.path, self.data, kind="dashboard", also_json=True)


class JobManager:
    def __init__(
        self,
        config: dict[str, Any],
        config_dir: Path,
        store: StateStore,
        accounts: AccountStore,
        models: ModelProfileStore,
        user_id: str = "",
    ) -> None:
        self.config = config
        self.config_dir = config_dir
        self.store = store
        self.accounts = accounts
        self.models = models
        self.user_id = str(user_id or "")
        # Concurrent job workers: default 3, hard-capped at 3 to protect API/Chrome load.
        configured_workers = int(config.get("dashboard", {}).get("job_workers", 3) or 3)
        self.job_workers = max(1, min(3, configured_workers))
        prefix = f"toutiao-job-{(self.user_id or 'sys')[:8]}"
        self.executor = ThreadPoolExecutor(max_workers=self.job_workers, thread_name_prefix=prefix)
        LOG.info("JobManager workers=%s user=%s", self.job_workers, self.user_id or "-")

    def create(
        self,
        topic: str,
        guidance: str = "",
        word_count: int | None = None,
        auto_action: str | None = None,
        account_id: str | None = None,
        topic_meta: dict[str, Any] | None = None,
        content_type: str = "article",
        video_duration: int | None = None,
        video_aspect_ratio: str | None = None,
    ) -> dict[str, Any]:
        account_state = self.accounts.snapshot()
        target_account_id = str(account_id or account_state.get("active_id") or "")
        target_account = next(
            (
                account
                for account in account_state.get("accounts", [])
                if account.get("id") == target_account_id
            ),
            None,
        )
        if account_id and target_account is None:
            raise KeyError(account_id)
        topic_meta = topic_meta or {}
        job = {
            "id": uuid.uuid4().hex[:12],
            "owner_user_id": self.user_id,
            "topic": topic.strip(),
            "guidance": guidance.strip(),
            "word_count": word_count,
            "content_type": content_type,
            "video_duration": video_duration,
            "video_aspect_ratio": video_aspect_ratio or "16:9",
            "status": "queued",
            "stage": "queued",
            "progress": 5,
            "title": "",
            "summary": "",
            "tags": [],
            "article_path": "",
            "cover_path": "",
            "video_path": "",
            "error": "",
            "publish_mode": auto_action,
            "account_id": target_account_id,
            "account_name": str(target_account.get("name") or "") if target_account else "",
            "topic_id": str(topic_meta.get("id") or ""),
            "topic_category": str(topic_meta.get("category") or ""),
            "topic_source": str(topic_meta.get("source") or ""),
            "topic_source_keys": list(topic_meta.get("source_keys") or []),
            "topic_url": str(topic_meta.get("url") or ""),
            "activity_id": str(topic_meta.get("activity_id") or ""),
            "activity_title": str(topic_meta.get("activity_title") or ""),
            "activity_introduction": str(topic_meta.get("activity_introduction") or ""),
            "activity_reward": str(topic_meta.get("activity_reward") or ""),
            "activity_max_award": int(topic_meta.get("activity_max_award") or 0),
            "activity_repeat_mode": str(topic_meta.get("activity_repeat_mode") or ""),
            "activity_match_score": float(topic_meta.get("activity_match_score") or 0),
            "activity_forum_id": str(topic_meta.get("activity_forum_id") or ""),
            "activity_content_type": str(topic_meta.get("activity_content_type") or ""),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self.store.add_job(job)
        self.executor.submit(self._generate, job["id"], auto_action)
        return job

    def publish(self, job_id: str, mode: str) -> dict[str, Any]:
        job = self._require_job(job_id)
        if not job.get("article_path"):
            raise ValueError("内容仍在生成")
        if job.get("content_type") == "video" and not job.get("video_path"):
            raise ValueError("视频仍在生成")
        updated = self.store.update_job(
            job_id,
            status="publish-queued",
            stage="publish-queued",
            progress=84,
            publish_mode=mode,
            error="",
        )
        self.executor.submit(self._publish, job_id, mode)
        return updated

    def update_article(self, job_id: str, payload: "DraftUpdate") -> dict[str, Any]:
        job = self._require_job(job_id)
        article_path = Path(str(job.get("article_path", "")))
        if not article_path.is_file():
            raise ValueError("草稿文件尚未生成")
        article = Article.load(article_path)
        updated_article = Article(
            topic=article.topic,
            title=(payload.title if payload.title is not None else article.title).strip(),
            summary=(payload.summary if payload.summary is not None else article.summary).strip(),
            body_markdown=(
                payload.body_markdown if payload.body_markdown is not None else article.body_markdown
            ).strip(),
            tags=(payload.tags if payload.tags is not None else article.tags)[:5],
            generated_at=article.generated_at,
        )
        if not 5 <= len(updated_article.title) <= 30:
            raise ValueError("标题长度需要保持在 5-30 个字符")
        if len(updated_article.body_markdown) < 100:
            raise ValueError("正文内容过短")
        article_path.write_text(
            json.dumps(updated_article.to_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.store.update_job(
            job_id,
            title=updated_article.title,
            summary=updated_article.summary,
            tags=updated_article.tags,
        )

    def read_article(self, job_id: str) -> dict[str, Any]:
        job = self._require_job(job_id)
        path = Path(str(job.get("article_path", "")))
        if not path.is_file():
            raise ValueError("草稿文件尚未生成")
        return Article.load(path).to_json()

    def regenerate(self, job_id: str, target: str = "all") -> dict[str, Any]:
        """Regenerate article text, cover image, video, or the full pipeline."""
        job = self._require_job(job_id)
        target = str(target or "all").strip().lower()
        if target not in {"article", "cover", "video", "all"}:
            raise ValueError("不支持的重新生成类型")
        busy = {
            "queued",
            "generating",
            "cover-generating",
            "video-requesting",
            "video-generating",
            "video-downloading",
            "video-ready",
            "publish-queued",
            "publishing",
            "video-auth",
            "video-uploading",
            "video-processing",
            "video-publishing",
        }
        if str(job.get("status") or "") in busy:
            raise ValueError("任务正在处理中，请稍后再重新生成")
        content_type = str(job.get("content_type") or "article")
        if target == "cover" and content_type == "video":
            raise ValueError("视频任务请重新生成视频")
        if target == "video" and content_type != "video":
            raise ValueError("图文任务请重新生成封面")
        if target in {"cover", "video"} and not job.get("article_path"):
            raise ValueError("请先生成文案后再单独重新生成媒体")
        if target == "cover":
            updated = self.store.update_job(
                job_id,
                status="cover-generating",
                stage="cover-generating",
                progress=55,
                error="",
                publish_mode=None,
            )
            self.executor.submit(self._regenerate_cover, job_id)
            return updated
        if target == "video":
            updated = self.store.update_job(
                job_id,
                status="video-generating",
                stage="video-generating",
                progress=60,
                error="",
                publish_mode=None,
            )
            self.executor.submit(self._regenerate_video, job_id)
            return updated
        # article or all
        if target == "article":
            # keep existing media paths; only rewrite text
            updated = self.store.update_job(
                job_id,
                status="queued",
                stage="queued",
                progress=5,
                error="",
                publish_mode=None,
            )
            self.executor.submit(self._regenerate_article, job_id, keep_media=True)
            return updated
        updated = self.store.update_job(
            job_id,
            status="queued",
            stage="queued",
            progress=5,
            error="",
            title="",
            summary="",
            tags=[],
            article_path="",
            cover_path="",
            video_path="",
            publish_mode=None,
        )
        self.executor.submit(self._generate, job_id, None)
        return updated

    def _regenerate_article(self, job_id: str, *, keep_media: bool = True) -> None:
        try:
            job = self._require_job(job_id)
            self.store.update_job(
                job_id, status="generating", stage="generating", progress=20, error=""
            )
            effective_config = self.models.apply(self.config)
            if job.get("word_count"):
                effective_config.setdefault("content", {})["word_count"] = int(job["word_count"])
            article = ArticleGenerator(effective_config).generate(job["topic"], job.get("guidance", ""))
            article_path = save_article(article, effective_config, self.config_dir, None)
            content_type = str(job.get("content_type") or "article")
            if keep_media:
                # keep existing media files; mark ready if media exists, else continue media gen
                has_media = bool(job.get("video_path") if content_type == "video" else job.get("cover_path"))
                self.store.update_job(
                    job_id,
                    status="ready" if has_media else (
                        "video-generating" if content_type == "video" else "cover-generating"
                    ),
                    stage="ready" if has_media else (
                        "video-generating" if content_type == "video" else "cover-generating"
                    ),
                    progress=82 if has_media and content_type == "video" else (80 if has_media else (60 if content_type == "video" else 55)),
                    title=article.title,
                    summary=article.summary,
                    tags=article.tags,
                    article_path=str(article_path),
                    error="",
                )
                if has_media:
                    return
                if content_type == "video":
                    self._regenerate_video(job_id)
                else:
                    self._regenerate_cover(job_id)
                return
            # full pipeline after article
            self.store.update_job(
                job_id,
                status="video-generating" if content_type == "video" else "cover-generating",
                stage="video-generating" if content_type == "video" else "cover-generating",
                progress=60 if content_type == "video" else 55,
                title=article.title,
                summary=article.summary,
                tags=article.tags,
                article_path=str(article_path),
                cover_path="",
                video_path="",
                error="",
            )
            if content_type == "video":
                self._regenerate_video(job_id)
            else:
                self._regenerate_cover(job_id)
        except Exception as exc:
            LOG.exception("Article regeneration failed: %s", job_id)
            self.store.update_job(
                job_id, status="error", stage="error", progress=100, error=str(exc)
            )

    def _regenerate_cover(self, job_id: str) -> None:
        try:
            job = self._require_job(job_id)
            article_path = Path(str(job.get("article_path") or ""))
            if not article_path.is_file():
                raise ValueError("草稿文件尚未生成")
            self.store.update_job(
                job_id,
                status="cover-generating",
                stage="cover-generating",
                progress=55,
                error="",
            )
            article = Article.load(article_path)
            effective_config = self.models.apply(self.config)
            cover_path = CoverGenerator(effective_config, self.config_dir).generate(article)
            self.store.update_job(
                job_id,
                status="ready",
                stage="ready",
                progress=80,
                cover_path=str(cover_path),
                error="",
            )
        except Exception as exc:
            LOG.exception("Cover regeneration failed: %s", job_id)
            self.store.update_job(
                job_id, status="error", stage="error", progress=100, error=str(exc)
            )

    def _regenerate_video(self, job_id: str) -> None:
        try:
            job = self._require_job(job_id)
            article_path = Path(str(job.get("article_path") or ""))
            if not article_path.is_file():
                raise ValueError("草稿文件尚未生成")
            self.store.update_job(
                job_id,
                status="video-generating",
                stage="video-generating",
                progress=60,
                error="",
            )
            article = Article.load(article_path)
            effective_config = self.models.apply(self.config)
            video_path = VideoGenerator(effective_config, self.config_dir).generate(
                article,
                job.get("guidance", ""),
                duration=job.get("video_duration"),
                aspect_ratio=job.get("video_aspect_ratio"),
                progress=lambda stage, percent: self.store.update_job(
                    job_id, status=stage, stage=stage, progress=percent
                ),
            )
            self.store.update_job(
                job_id,
                status="ready",
                stage="ready",
                progress=82,
                video_path=str(video_path),
                error="",
            )
        except Exception as exc:
            LOG.exception("Video regeneration failed: %s", job_id)
            self.store.update_job(
                job_id, status="error", stage="error", progress=100, error=str(exc)
            )

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def recover_pending(self) -> list[dict[str, str]]:
        """Resume durable jobs whose worker futures were lost during a restart."""
        jobs = list(reversed(self.store.snapshot()["jobs"]))
        recovered: list[dict[str, str]] = []

        # Uploads are resumed first so completed local content is not blocked by AI generation.
        for job in jobs:
            if job.get("status") not in {
                "publish-queued",
                "publishing",
                "video-auth",
                "video-uploading",
                "video-processing",
                "video-publishing",
            }:
                continue
            job_id = str(job["id"])
            mode = str(job.get("publish_mode") or "")
            content_path = Path(
                str(
                    job.get("video_path")
                    if job.get("content_type") == "video"
                    else job.get("article_path")
                )
            )
            if mode not in {"draft", "publish"} or not content_path.is_file():
                self.store.update_job(
                    job_id,
                    status="error",
                    stage="error",
                    progress=100,
                    error="重启后恢复上传失败：本地内容或上传模式无效",
                )
                continue
            self.store.update_job(
                job_id,
                status="publish-queued",
                stage="publish-queued",
                progress=84,
                error="",
            )
            self.executor.submit(self._publish, job_id, mode)
            recovered.append({"id": job_id, "action": mode})

        for job in jobs:
            status = str(job.get("status") or "")
            if status not in {
                "queued",
                "generating",
                "cover-generating",
                "video-requesting",
                "video-generating",
                "video-downloading",
            }:
                continue
            job_id = str(job["id"])
            auto_action = str(job.get("publish_mode") or "") or None
            article_path = Path(str(job.get("article_path") or ""))
            if job.get("content_type") == "video" and article_path.is_file():
                self.store.update_job(
                    job_id,
                    status="video-generating",
                    stage="video-generating",
                    progress=max(60, int(job.get("progress") or 0)),
                    error="",
                )
                self.executor.submit(self._resume_video, job_id, auto_action)
                recovered.append({"id": job_id, "action": "video"})
            elif status == "cover-generating" and article_path.is_file():
                self.store.update_job(
                    job_id,
                    status="cover-generating",
                    stage="cover-generating",
                    progress=55,
                    error="",
                )
                self.executor.submit(self._resume_cover, job_id, auto_action)
                recovered.append({"id": job_id, "action": "cover"})
            else:
                self.store.update_job(
                    job_id, status="queued", stage="queued", progress=5, error=""
                )
                self.executor.submit(self._generate, job_id, auto_action)
                recovered.append({"id": job_id, "action": "generate"})
        return recovered

    def _generate(self, job_id: str, auto_action: str | None) -> None:
        try:
            job = self._require_job(job_id)
            self.store.update_job(
                job_id, status="generating", stage="generating", progress=20, error=""
            )
            effective_config = self.models.apply(self.config)
            if job.get("word_count"):
                effective_config.setdefault("content", {})["word_count"] = int(job["word_count"])
            article = ArticleGenerator(effective_config).generate(job["topic"], job.get("guidance", ""))
            article_path = save_article(article, effective_config, self.config_dir, None)
            self.store.update_job(
                job_id,
                status=(
                    "video-generating"
                    if job.get("content_type") == "video"
                    else "cover-generating"
                ),
                stage=(
                    "video-generating"
                    if job.get("content_type") == "video"
                    else "cover-generating"
                ),
                progress=60 if job.get("content_type") == "video" else 55,
                title=article.title,
                summary=article.summary,
                tags=article.tags,
                article_path=str(article_path),
            )
            if job.get("content_type") == "video":
                video_path = VideoGenerator(effective_config, self.config_dir).generate(
                    article,
                    job.get("guidance", ""),
                    duration=job.get("video_duration"),
                    aspect_ratio=job.get("video_aspect_ratio"),
                    progress=lambda stage, percent: self.store.update_job(
                        job_id, status=stage, stage=stage, progress=percent
                    ),
                )
                self.store.update_job(
                    job_id,
                    status="ready",
                    stage="ready",
                    progress=82,
                    video_path=str(video_path),
                )
                if auto_action in {"draft", "publish"}:
                    self._publish(job_id, auto_action)
                return
            cover_path = CoverGenerator(effective_config, self.config_dir).generate(article)
            self.store.update_job(
                job_id,
                status="ready",
                stage="ready",
                progress=80,
                cover_path=str(cover_path),
            )
            if auto_action in {"draft", "publish"}:
                self._publish(job_id, auto_action)
        except Exception as exc:
            LOG.exception("Generation job failed: %s", job_id)
            self.store.update_job(
                job_id, status="error", stage="error", progress=100, error=str(exc)
            )

    def _resume_cover(self, job_id: str, auto_action: str | None) -> None:
        try:
            job = self._require_job(job_id)
            article = Article.load(Path(str(job["article_path"])))
            effective_config = self.models.apply(self.config)
            cover_path = CoverGenerator(effective_config, self.config_dir).generate(article)
            self.store.update_job(
                job_id,
                status="ready",
                stage="ready",
                progress=80,
                cover_path=str(cover_path),
                error="",
            )
            if auto_action in {"draft", "publish"}:
                self._publish(job_id, auto_action)
        except Exception as exc:
            LOG.exception("Cover recovery failed: %s", job_id)
            self.store.update_job(
                job_id, status="error", stage="error", progress=100, error=str(exc)
            )

    def _resume_video(self, job_id: str, auto_action: str | None) -> None:
        try:
            job = self._require_job(job_id)
            article = Article.load(Path(str(job["article_path"])))
            effective_config = self.models.apply(self.config)
            video_path = VideoGenerator(effective_config, self.config_dir).generate(
                article,
                job.get("guidance", ""),
                duration=job.get("video_duration"),
                aspect_ratio=job.get("video_aspect_ratio"),
                progress=lambda stage, percent: self.store.update_job(
                    job_id, status=stage, stage=stage, progress=percent
                ),
            )
            self.store.update_job(
                job_id,
                status="ready",
                stage="ready",
                progress=82,
                video_path=str(video_path),
                error="",
            )
            if auto_action in {"draft", "publish"}:
                self._publish(job_id, auto_action)
        except Exception as exc:
            LOG.exception("Video recovery failed: %s", job_id)
            self.store.update_job(
                job_id, status="error", stage="error", progress=100, error=str(exc)
            )

    def _publish(self, job_id: str, mode: str) -> None:
        try:
            job = self._require_job(job_id)
            self.store.update_job(
                job_id, status="publishing", stage="publishing", progress=88, error=""
            )
            article = Article.load(Path(job["article_path"]))
            cover = Path(job["cover_path"]) if job.get("cover_path") else None
            account_id = str(job.get("account_id") or "")
            credentials = (
                self.accounts.credentials(account_id)
                if account_id
                else self.accounts.active_credentials()
            )
            if credentials is None:
                raise PublisherError("任务绑定的头条账号不存在或登录信息已失效")
            if job.get("content_type") == "video":
                with ToutiaoVideoClient(
                    self.config,
                    self.config_dir,
                    cookie_override=credentials.get("cookies") if credentials else None,
                    headers_override=credentials.get("headers") if credentials else None,
                ) as publisher:
                    report = publisher.publish(
                        article,
                        Path(str(job["video_path"])),
                        mode,
                        landscape=str(job.get("video_aspect_ratio") or "16:9") != "9:16",
                        activity_tag=str(job.get("activity_id") or 0),
                        progress=lambda stage, percent: self.store.update_job(
                            job_id, status=stage, stage=stage, progress=percent
                        ),
                    )
            else:
                with ToutiaoProtocolClient(
                    self.config,
                    self.config_dir,
                    cookie_override=credentials.get("cookies") if credentials else None,
                    headers_override=credentials.get("headers") if credentials else None,
                ) as publisher:
                    report = publisher.publish(
                        article,
                        mode,
                        cover,
                        dry_run=False,
                        activity_tag=str(job.get("activity_id") or 0),
                    )
            self.store.update_job(
                job_id,
                status="completed",
                stage="completed",
                progress=100,
                report=report,
                publish_mode=mode,
            )
        except Exception as exc:
            LOG.exception("Publish job failed: %s", job_id)
            self.store.update_job(
                job_id, status="error", stage="error", progress=100, error=str(exc)
            )

    def _require_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job


class AutomationEngine:
    def __init__(
        self,
        store: StateStore,
        hot_topics: HotTopicService,
        jobs: JobManager,
        accounts: AccountStore,
        config: dict[str, Any] | None = None,
        config_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.hot_topics = hot_topics
        self.jobs = jobs
        self.accounts = accounts
        self.config = config or {}
        self.config_dir = config_dir or Path(".")
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="toutiao-automation")

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def update(
        self,
        account_id: str,
        enabled: bool,
        interval_minutes: int,
        mode: str,
        pick_count: int,
        categories: list[str],
        sources: list[str],
        content_type: str = "article",
        video_duration: int = 15,
        auto_claim_challenges: bool = True,
    ) -> dict[str, Any]:
        if self.accounts.credentials(account_id) is None:
            raise KeyError(account_id)
        content_type = str(content_type or "article").strip().lower()
        if content_type not in {"article", "video"}:
            content_type = "article"
        changes = {
            "enabled": enabled,
            "interval_minutes": max(5, interval_minutes),
            "mode": mode,
            "content_type": content_type,
            "video_duration": max(2, min(60, int(video_duration or 15))),
            "auto_claim_challenges": bool(auto_claim_challenges),
            "pick_count": max(1, min(5, pick_count)),
            "categories": list(dict.fromkeys(categories)),
            "sources": list(dict.fromkeys(sources)),
            "next_run": now_iso() if enabled else None,
            "last_error": "",
        }
        self.store.update_automation_account(account_id, **changes)
        self.wake_event.set()
        return self.status()

    def status(self) -> dict[str, Any]:
        profiles = self._sync_accounts()
        account_state = self.accounts.snapshot()
        public_accounts = {item["id"]: item for item in account_state.get("accounts", [])}
        rows = []
        for account_id, profile in profiles.items():
            account = public_accounts.get(account_id, {})
            rows.append(
                {
                    "account_id": account_id,
                    "account_name": str(account.get("name") or "头条账号"),
                    "account_avatar": str(account.get("avatar") or ""),
                    **copy.deepcopy(profile),
                }
            )
        return {
            "accounts": rows,
            "enabled_count": sum(1 for profile in rows if profile.get("enabled")),
            "total_accounts": len(rows),
        }

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            profiles = self._sync_accounts()
            due_profiles = []
            for account_id, profile in profiles.items():
                if not profile.get("enabled"):
                    continue
                next_run = profile.get("next_run")
                due = not next_run or datetime.fromisoformat(next_run) <= datetime.now(timezone.utc)
                if due:
                    due_profiles.append((account_id, profile))
            if due_profiles:
                topics = self.hot_topics.fetch(force=True)
                for account_id, profile in due_profiles:
                    self._run_account(account_id, profile, topics)
            self.wake_event.wait(5)
            self.wake_event.clear()

    def _claim_opportunities(
        self,
        account_id: str,
        content_type: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        credentials = self.accounts.credentials(account_id)
        if credentials is None:
            raise PublisherError("任务账号尚未登录，无法读取创作活动")
        requested_biz_id = 2 if content_type == "video" else 1
        with ToutiaoChallengeClient(
            self.config,
            self.config_dir,
            cookie_override=credentials.get("cookies"),
            headers_override=credentials.get("headers"),
        ) as client:
            discovered: list[dict[str, Any]] = []
            for biz_id, media_type in ((1, "article"), (2, "video")):
                activities = client.list_all(biz_id=biz_id, part_status=0)
                for activity in activities:
                    client.enrich_activity(activity)
                    activity["biz_id"] = biz_id
                    activity["content_type"] = media_type
                    discovered.append(activity)
        unique: dict[str, dict[str, Any]] = {}
        for activity in discovered:
            activity_id = str(activity.get("id") or activity.get("activity_id") or "").strip()
            if not activity_id:
                continue
            current = unique.get(activity_id)
            if current is None or int(activity.get("biz_id") or 0) == requested_biz_id:
                unique[activity_id] = activity
        activities = list(unique.values())
        claim_rows = [
            {
                **activity,
                "activity_id": str(activity.get("id") or ""),
            }
            for activity in activities
            if str(activity.get("status") or "active") == "active"
        ]
        accepted = self.store.accept_challenges(account_id, claim_rows)
        compatible = [
            activity
            for activity in claim_rows
            if str(activity.get("content_type") or "") == content_type
        ]
        return compatible, accepted

    @staticmethod
    def _select_opportunity(
        topic: Mapping[str, Any],
        activities: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not activities:
            return None
        ranked = [
            (score_activity_for_topic(topic, activity), activity_reward_value(activity), activity)
            for activity in activities
            if str(activity.get("status") or "active") == "active"
        ]
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        score, _, activity = ranked[0]
        selected = copy.deepcopy(activity)
        selected["match_score"] = score
        return selected

    def _run_account(
        self,
        account_id: str,
        automation: dict[str, Any],
        topics: list[dict[str, Any]],
    ) -> None:
        try:
            categories = set(str(item) for item in automation.get("categories", []))
            sources = set(str(item) for item in automation.get("sources", []))
            content_type = str(automation.get("content_type") or "article").strip().lower()
            if content_type not in {"article", "video"}:
                content_type = "article"
            video_duration = max(2, min(60, int(automation.get("video_duration") or 15)))
            opportunities: list[dict[str, Any]] = []
            accepted = {"new_count": 0, "accepted_count": 0}
            challenge_error = ""
            if bool(automation.get("auto_claim_challenges", True)):
                try:
                    opportunities, accepted = self._claim_opportunities(account_id, content_type)
                except Exception as exc:
                    challenge_error = str(exc)
                    LOG.warning("Challenge sync failed for account %s: %s", account_id, exc)
            existing = {
                job["topic"]
                for job in self.store.snapshot()["jobs"]
                if str(job.get("account_id") or "") == account_id
            }
            selected = []
            for topic in topics:
                if topic["title"] in existing:
                    continue
                if categories and topic.get("category") not in categories:
                    continue
                if sources and not sources.intersection(topic.get("source_keys", [])):
                    continue
                selected.append(topic)
                if len(selected) >= int(automation.get("pick_count", 1)):
                    break
            for topic in selected:
                opportunity = self._select_opportunity(topic, opportunities)
                angle = str(topic.get("angle") or "").strip()
                if not angle:
                    try:
                        from hot_topics import suggest_writing_angle

                        angle = suggest_writing_angle(
                            topic["title"],
                            str(topic.get("category") or ""),
                            list(topic.get("source_keys") or []),
                            int(topic.get("hot_value") or 0),
                            str(topic.get("label") or ""),
                        )
                    except Exception:
                        angle = "结合实时事件背景，突出对普通读者的影响，避免仅复述新闻"
                type_hint = (
                    f"内容形态：短视频（约 {video_duration} 秒），偏口播与画面信息。"
                    if content_type == "video"
                    else "内容形态：图文，配封面图。"
                )
                activity_hint = ""
                topic_meta = dict(topic)
                if opportunity:
                    activity_id = str(opportunity.get("id") or opportunity.get("activity_id") or "")
                    activity_title = str(opportunity.get("title") or "创作活动")
                    reward = str(opportunity.get("reward_label") or "按规则发放")
                    activity_intro = str(opportunity.get("introduction") or "").strip()
                    activity_hint = (
                        f"【变现任务】优先参与“{activity_title}”，奖励：{reward}。"
                        f"活动要求：{activity_intro or '按活动详情完成投稿'}。"
                        "作品发布时关联该活动，不要偏离活动主题。"
                    )
                    topic_meta.update(
                        {
                            "activity_id": activity_id,
                            "activity_title": activity_title,
                            "activity_introduction": activity_intro,
                            "activity_reward": reward,
                            "activity_max_award": activity_reward_value(opportunity),
                            "activity_repeat_mode": str(opportunity.get("repeat_mode") or ""),
                            "activity_match_score": float(opportunity.get("match_score") or 0),
                            "activity_forum_id": str(opportunity.get("forum_id") or ""),
                            "activity_content_type": content_type,
                        }
                    )
                self.jobs.create(
                    topic["title"],
                    guidance=(
                        f"{angle} {type_hint} {activity_hint} "
                        f"热点分类：{topic.get('category', '其他')}；来源：{topic.get('source', '全网')}。"
                    ),
                    auto_action=str(automation.get("mode", "draft")),
                    account_id=account_id,
                    topic_meta=topic_meta,
                    content_type=content_type,
                    video_duration=video_duration if content_type == "video" else None,
                )
            interval = max(5, int(automation.get("interval_minutes", 60)))
            next_run = datetime.now(timezone.utc) + timedelta(minutes=interval)
            self.store.update_automation_account(
                account_id,
                last_run=now_iso(),
                next_run=next_run.isoformat(),
                last_error=challenge_error,
                last_selected=len(selected),
                last_challenge_sync=now_iso() if bool(automation.get("auto_claim_challenges", True)) else "",
                last_challenge_claimed=int(accepted.get("new_count") or 0),
                challenge_opportunity_count=len(opportunities),
                challenge_top_reward=(
                    str(max(opportunities, key=activity_reward_value).get("reward_label") or "")
                    if opportunities
                    else ""
                ),
                challenge_top_title=(
                    str(max(opportunities, key=activity_reward_value).get("title") or "")
                    if opportunities
                    else ""
                ),
            )
        except Exception as exc:
            LOG.exception("Automation cycle failed for account %s", account_id)
            next_run = datetime.now(timezone.utc) + timedelta(minutes=10)
            self.store.update_automation_account(
                account_id,
                last_run=now_iso(),
                next_run=next_run.isoformat(),
                last_error=str(exc),
                last_selected=0,
            )

    def _sync_accounts(self) -> dict[str, dict[str, Any]]:
        account_state = self.accounts.snapshot()
        account_rows = account_state.get("accounts", [])
        valid_ids = [str(account["id"]) for account in account_rows]
        active_id = str(account_state.get("active_id") or "")
        active_account = next(
            (account for account in account_rows if str(account.get("id")) == active_id),
            {},
        )
        self.store.bind_unassigned_jobs(active_id, str(active_account.get("name") or "头条账号"))
        automation = self.store.snapshot()["automation"]
        defaults = copy.deepcopy(automation.get("defaults", {}))
        stored = copy.deepcopy(automation.get("accounts", {}))
        legacy = automation.get("legacy") if isinstance(automation.get("legacy"), dict) else None
        profiles: dict[str, dict[str, Any]] = {}
        for account_id in valid_ids:
            if account_id in stored:
                profile = copy.deepcopy(defaults)
                profile.update(stored[account_id])
            elif legacy is not None and account_id == active_id:
                profile = copy.deepcopy(defaults)
                profile.update(legacy)
            else:
                profile = copy.deepcopy(defaults)
                profile["enabled"] = bool(defaults.get("enabled")) if account_id == active_id else False
                profile["next_run"] = now_iso() if profile["enabled"] else None
            profile.setdefault("categories", [])
            profile.setdefault("sources", [])
            profile.setdefault("last_error", "")
            profile.setdefault("last_selected", 0)
            profile.setdefault("auto_claim_challenges", True)
            profile.setdefault("last_challenge_sync", "")
            profile.setdefault("last_challenge_claimed", 0)
            profile.setdefault("challenge_opportunity_count", 0)
            profile.setdefault("challenge_top_reward", "")
            profile.setdefault("challenge_top_title", "")
            profile.setdefault("content_type", "article")
            if profile.get("content_type") not in {"article", "video"}:
                profile["content_type"] = "article"
            profile.setdefault("video_duration", 15)
            try:
                profile["video_duration"] = max(2, min(60, int(profile.get("video_duration") or 15)))
            except Exception:
                profile["video_duration"] = 15
            profiles[account_id] = profile
        if profiles != stored or legacy is not None:
            self.store.replace_automation_accounts(profiles)
        return profiles


class UserRuntime:
    def __init__(
        self,
        config: dict[str, Any],
        config_dir: Path,
        hot_topics: HotTopicService,
        user_id: str = "",
    ) -> None:
        self.user_id = str(user_id or "")
        self.config = config
        self.config_dir = config_dir
        state_path = resolve_path(
            config_dir,
            config.get("dashboard", {}).get("state_file", "./state/dashboard.json"),
        )
        assert state_path is not None
        # tenant path must never fall back to shared global dashboard
        self.tenant_root = state_path.parent
        self.tenant_root.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(state_path, config.get("automation", {}))
        self.accounts = AccountStore(config, config_dir)
        self.models = ModelProfileStore(config, config_dir)
        self.qr_login = QRLoginManager(config, self.accounts)
        self.jobs = JobManager(
            config,
            config_dir,
            self.store,
            self.accounts,
            self.models,
            user_id=self.user_id,
        )
        self.automation = AutomationEngine(
            self.store,
            hot_topics,
            self.jobs,
            self.accounts,
            config,
            config_dir,
        )
        self.started = False
        self.lock = threading.RLock()

    def start(self) -> None:
        with self.lock:
            if self.started:
                return
            recovered = self.jobs.recover_pending()
            if recovered:
                LOG.info("Recovered %d interrupted user job(s): %s", len(recovered), recovered)
            self.automation.start()
            self.started = True

    def stop(self) -> None:
        with self.lock:
            if not self.started:
                return
            self.automation.stop()
            self.jobs.shutdown()
            self.started = False


class UserRuntimeManager:
    def __init__(
        self,
        config: dict[str, Any],
        config_dir: Path,
        hot_topics: HotTopicService,
    ) -> None:
        self.base_config = config
        self.config_dir = config_dir
        self.hot_topics = hot_topics
        state_path = resolve_path(
            config_dir,
            config.get("dashboard", {}).get("state_file", "./state/dashboard.json"),
        )
        assert state_path is not None
        self.legacy_state_path = state_path
        self.state_root = state_path.parent
        self.tenants_root = self.state_root / "tenants"
        self.tenants_root.mkdir(parents=True, exist_ok=True)
        self.runtimes: dict[str, UserRuntime] = {}
        self.lock = threading.RLock()

    def tenant_root(self, user_id: str) -> Path:
        return self.tenants_root / user_id

    def tenant_config(self, user_id: str) -> dict[str, Any]:
        root = self.tenant_root(user_id)
        config = copy.deepcopy(self.base_config)
        dashboard = config.setdefault("dashboard", {})
        dashboard.update(
            {
                "state_file": str(root / "dashboard.json"),
                "accounts_file": str(root / "accounts.json"),
                "models_file": str(root / "models.json"),
                "secret_key_file": str(root / ".secret-key"),
            }
        )
        config.setdefault("cover", {})["output_dir"] = str(root / "covers")
        config.setdefault("video", {})["output_dir"] = str(root / "videos")
        config.setdefault("upload", {})["draft_dir"] = str(root / "drafts")
        toutiao = config.setdefault("toutiao", {})
        toutiao["cookie_file"] = str(root / "toutiao-cookie.txt")
        toutiao["headers_file"] = str(root / "toutiao-headers.json")
        toutiao["chrome_profile_root"] = str(root / "chrome-protocol-profiles")
        if isinstance(config.get("batch"), dict):
            config["batch"]["ledger_file"] = str(root / "published_topics.json")
        return config

    def ensure_tenant_scaffold(self, user_id: str) -> Path:
        """Create per-user directories/files so modules never share global state."""
        root = self.tenant_root(user_id)
        root.mkdir(parents=True, exist_ok=True)
        for name in ("covers", "videos", "drafts", "chrome-protocol-profiles"):
            (root / name).mkdir(parents=True, exist_ok=True)
        # Do not seed from global shared files for non-legacy users.
        return root

    def get(self, user_id: str) -> UserRuntime:
        with self.lock:
            runtime = self.runtimes.get(user_id)
            if runtime is None:
                self.ensure_tenant_scaffold(user_id)
                runtime = UserRuntime(
                    self.tenant_config(user_id),
                    self.config_dir,
                    self.hot_topics,
                    user_id=user_id,
                )
                self.runtimes[user_id] = runtime
            runtime.start()
            return runtime

    def stop_user(self, user_id: str) -> None:
        with self.lock:
            runtime = self.runtimes.pop(user_id, None)
        if runtime:
            runtime.stop()

    def shutdown(self) -> None:
        with self.lock:
            runtimes = list(self.runtimes.values())
            self.runtimes.clear()
        for runtime in runtimes:
            runtime.stop()

    def migrate_legacy(self, user_id: str) -> None:
        root = self.tenant_root(user_id)
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".legacy-migrated"
        if marker.is_file():
            return
        account_path, model_path, key_path = storage_paths(self.base_config, self.config_dir)
        copies = (
            (self.legacy_state_path, root / "dashboard.json"),
            (account_path, root / "accounts.json"),
            (model_path, root / "models.json"),
            (key_path, root / ".secret-key"),
        )
        for source, target in copies:
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
        tenant_state = root / "dashboard.json"
        if tenant_state.is_file():
            try:
                payload = json.loads(tenant_state.read_text(encoding="utf-8"))
                for job in payload.get("jobs", []):
                    for field, directory in (
                        ("article_path", root / "drafts"),
                        ("cover_path", root / "covers"),
                        ("video_path", root / "videos"),
                    ):
                        source_value = str(job.get(field) or "")
                        if not source_value:
                            continue
                        source = Path(source_value)
                        target = directory / source.name
                        if source.is_file():
                            directory.mkdir(parents=True, exist_ok=True)
                            if not target.exists():
                                shutil.copy2(source, target)
                            job[field] = str(target)
                tenant_state.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except (OSError, json.JSONDecodeError):
                LOG.exception("Could not migrate legacy dashboard data for %s", user_id)
                raise
        marker.write_text(now_iso() + "\n", encoding="utf-8")



def guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }
    return mapping.get(suffix, "application/octet-stream")


def resolve_job_media_path(job: Mapping[str, Any], kind: str, runtime: "UserRuntime") -> Path | None:
    """Resolve cover/video file for a job, with tenant basename fallback."""
    field = "cover_path" if kind == "cover" else "video_path"
    raw = str(job.get(field) or "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    name = Path(raw).name if raw else ""
    tenant_root = getattr(runtime, "tenant_root", None)
    if tenant_root is not None and name:
        folder = "covers" if kind == "cover" else "videos"
        candidates.append(Path(tenant_root) / folder / name)
    # also try config output dirs
    try:
        if kind == "cover":
            out = runtime.config.get("cover", {}).get("output_dir")
        else:
            out = runtime.config.get("video", {}).get("output_dir")
        if out and name:
            candidates.append(Path(out) / name)
    except Exception:
        pass
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def enrich_job_media(job: dict[str, Any], runtime: "UserRuntime") -> dict[str, Any]:
    item = dict(job)
    cover = resolve_job_media_path(item, "cover", runtime)
    video = resolve_job_media_path(item, "video", runtime)
    item["has_cover"] = cover is not None
    item["has_video"] = video is not None
    if cover is not None:
        item["cover_path"] = str(cover)
    if video is not None:
        item["video_path"] = str(video)
    job_id = str(item.get("id") or "")
    stamp = str(item.get("updated_at") or "")
    item["cover_url"] = f"/api/jobs/{job_id}/cover?v={stamp}" if cover is not None else ""
    item["video_url"] = f"/api/jobs/{job_id}/video?v={stamp}" if video is not None else ""
    return item

class GenerateRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=120)
    guidance: str = Field(default="", max_length=500)
    word_count: int = Field(default=1200, ge=500, le=3000)
    auto_action: str | None = Field(default=None, pattern="^(draft|publish)$")
    account_id: str | None = Field(default=None, max_length=80)
    topic_id: str = Field(default="", max_length=80)
    topic_category: str = Field(default="", max_length=40)
    topic_source: str = Field(default="", max_length=200)
    topic_source_keys: list[str] = Field(default_factory=list, max_length=20)
    topic_url: str = Field(default="", max_length=1000)
    content_type: str = Field(default="article", pattern="^(article|video)$")
    video_duration: int = Field(default=15, ge=2, le=60)
    video_aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16|1:1)$")


class PublishRequest(BaseModel):
    mode: str = Field(pattern="^(draft|publish)$")


class ChallengeGenerateRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    biz_id: int = Field(default=1, ge=1, le=2)
    content_type: str = Field(default="article", pattern="^(article|video)$")
    word_count: int = Field(default=1200, ge=500, le=3000)
    auto_action: str | None = Field(default=None, pattern="^(draft|publish)$")
    video_duration: int = Field(default=15, ge=2, le=60)
    video_aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16|1:1)$")
    introduction: str = Field(default="", max_length=500)
    repeat_mode: str = Field(default="unknown", pattern="^(unknown|once|daily|weekly)$")
    repeat_reason: str = Field(default="", max_length=120)
    activity_url: str = Field(default="", max_length=2000)


class ChallengeAcceptRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    biz_id: int = Field(default=1, ge=1, le=2)
    repeat_mode: str = Field(default="unknown", pattern="^(unknown|once|daily|weekly)$")
    repeat_reason: str = Field(default="", max_length=120)
    activity_url: str = Field(default="", max_length=2000)


class ChallengeBatchAcceptRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    biz_id: int = Field(default=1, ge=1, le=2)
    part_status: int = Field(default=0, ge=0, le=2)
    category: str = Field(default="全部", max_length=40)
    query: str = Field(default="", max_length=120)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=100)


class DraftUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    body_markdown: str | None = None
    tags: list[str] | None = None


class RegenerateRequest(BaseModel):
    target: str = Field(default="all", pattern="^(article|cover|video|all)$")


class AutomationRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    enabled: bool
    interval_minutes: int = Field(ge=5, le=1440)
    mode: str = Field(pattern="^(draft|publish)$")
    content_type: str = Field(default="article", pattern="^(article|video)$")
    video_duration: int = Field(default=15, ge=2, le=60)
    auto_claim_challenges: bool = True
    pick_count: int = Field(ge=1, le=5)
    categories: list[str] = Field(default_factory=list, max_length=20)
    sources: list[str] = Field(default_factory=list, max_length=20)


class SessionRequest(BaseModel):
    cookie: str = Field(min_length=3)
    headers: dict[str, str] = Field(default_factory=dict)


class AccountRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class ModelProfileRequest(BaseModel):
    id: str | None = Field(default=None, pattern=r"^model-[A-Za-z0-9_-]{1,64}$")
    kind: str = Field(pattern="^(article|cover|video)$")
    name: str = Field(min_length=1, max_length=40)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=500)
    temperature: float = Field(default=0.7, ge=0, le=2)
    json_mode: bool = True
    size: str = Field(default="1536x1024", max_length=40)
    quality: str = Field(default="medium", max_length=40)
    duration: int = Field(default=15, ge=2, le=15)
    aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16|1:1)$")
    poll_interval: float = Field(default=5, ge=1, le=60)
    timeout: int = Field(default=900, ge=30, le=7200)
    create_path: str = Field(default="/videos", min_length=1, max_length=200)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AdminUserCreateRequest(RegisterRequest):
    role: str = Field(default="user", pattern="^(admin|user)$")
    enabled: bool = True


class AdminUserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


def create_app(config_path: Path) -> FastAPI:
    config_path = config_path.expanduser().resolve()
    config = load_toml(config_path)
    config_dir = config_path.parent
    state_path = resolve_path(config_dir, config.get("dashboard", {}).get("state_file", "./state/dashboard.json"))
    assert state_path is not None
    hot_topics = HotTopicService(config)
    init_db(state_path.parent / "app.db")
    migrate_state_tree(state_path.parent)
    users = UserStore(state_path.parent / "users.json")
    signer = SessionSigner(state_path.parent / ".auth-session-key")
    runtimes = UserRuntimeManager(config, config_dir, hot_topics)
    dashboard_config = config.get("dashboard", {})
    registration_enabled = bool(dashboard_config.get("registration_enabled", True))
    cookie_secure = bool(dashboard_config.get("cookie_secure", False))
    cookie_name = "toutiao_app_session"

    app = FastAPI(title="头条内容台", version="1.0.0")

    def resolve_user(request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(cookie_name, "")
        user_id = signer.verify(token) if token else None
        user = users.get(user_id) if user_id else None
        return user if user and user.get("enabled") else None

    def require_user(request: Request) -> dict[str, Any]:
        user = resolve_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return user

    def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    def runtime_for_user(user: dict[str, Any]) -> UserRuntime:
        marker = runtimes.tenant_root(str(user["id"])) / ".legacy-migrated"
        if users.count() == 1 and user.get("role") == "admin" and not marker.is_file():
            runtimes.migrate_legacy(str(user["id"]))
        return runtimes.get(str(user["id"]))

    def current_runtime(user: dict[str, Any] = Depends(require_user)) -> UserRuntime:
        return runtime_for_user(user)

    def set_session_cookie(response: Response, user_id: str) -> None:
        response.set_cookie(
            cookie_name,
            signer.issue(user_id),
            max_age=signer.ttl_seconds,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )

    @app.on_event("startup")
    def startup() -> None:
        for user in users.list_users():
            if user.get("enabled"):
                runtime_for_user(user)

    @app.on_event("shutdown")
    def shutdown() -> None:
        runtimes.shutdown()

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        user = resolve_user(request)
        return {
            "authenticated": user is not None,
            "user": user,
            "registration_open": registration_enabled or users.count() == 0,
        }

    @app.post("/api/auth/register", status_code=201)
    def register_user(request: RegisterRequest, response: Response) -> dict[str, Any]:
        if users.count() > 0 and not registration_enabled:
            raise HTTPException(status_code=403, detail="当前未开放注册")
        try:
            user, first_user = users.register(
                request.username,
                request.password,
                request.display_name,
            )
            if first_user:
                runtimes.migrate_legacy(str(user["id"]))
            runtime_for_user(user)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        set_session_cookie(response, str(user["id"]))
        return {"user": user}

    @app.post("/api/auth/login")
    def login_user(request: LoginRequest, response: Response) -> dict[str, Any]:
        try:
            user = users.authenticate(request.username, request.password)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        runtime_for_user(user)
        set_session_cookie(response, str(user["id"]))
        return {"user": user}

    @app.post("/api/auth/logout")
    def logout_user(response: Response) -> dict[str, bool]:
        response.delete_cookie(cookie_name, path="/")
        return {"logged_out": True}

    @app.get("/api/admin/users")
    def list_app_users(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
        rows = users.list_users()
        return {"users": rows, "count": len(rows)}

    @app.post("/api/admin/users", status_code=201)
    def create_app_user(
        request: AdminUserCreateRequest,
        _: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            user, _ = users.register(
                request.username,
                request.password,
                request.display_name,
                role=request.role,
                enabled=request.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if user.get("enabled"):
            runtime_for_user(user)
        return user

    @app.patch("/api/admin/users/{user_id}")
    def update_app_user(
        user_id: str,
        request: AdminUserUpdateRequest,
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        previous = users.get(user_id)
        if previous is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        try:
            user = users.update(
                user_id,
                actor_user_id=str(admin["id"]),
                display_name=request.display_name,
                role=request.role,
                enabled=request.enabled,
                password=request.password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if previous.get("enabled") and not user.get("enabled"):
            runtimes.stop_user(user_id)
        elif not previous.get("enabled") and user.get("enabled"):
            runtime_for_user(user)
        return user

    @app.delete("/api/admin/users/{user_id}")
    def delete_app_user(
        user_id: str,
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, bool]:
        try:
            deleted = users.delete(user_id, actor_user_id=str(admin["id"]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="用户不存在")
        runtimes.stop_user(user_id)
        return {"deleted": True}

    @app.get("/api/status")
    def status(
        user: dict[str, Any] = Depends(require_user),
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        snapshot = runtime.store.snapshot()
        model_status = runtime.models.active_summary()
        account_state = runtime.accounts.snapshot()
        protocol_session = credential_summary(runtime.config, config_dir)
        active_credentials = runtime.accounts.active_credentials()
        if active_credentials:
            protocol_session = {
                "configured": True,
                "source": "account",
                "cookie_count": len(active_credentials.get("cookies", {})),
                "header_count": len(active_credentials.get("headers", {})),
                "account_id": active_credentials["account"]["id"],
            }
        # hard isolate: only this tenant's jobs (and stamp missing owner)
        user_id = str(user.get("id") or runtime.user_id or "")
        jobs = []
        for job in snapshot.get("jobs", []):
            if not isinstance(job, dict):
                continue
            owner = str(job.get("owner_user_id") or "")
            if owner and owner != user_id:
                continue
            if not owner:
                job = {**job, "owner_user_id": user_id}
            jobs.append(enrich_job_media(job, runtime))
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        return {
            "api_key_configured": model_status["api_key_configured"],
            "api_key_env": model_status["api_key_env"],
            "protocol_session": protocol_session,
            "accounts": account_state,
            "models": runtime.models.snapshot(),
            "ai_model": model_status["article_model"],
            "cover_model": model_status["cover_model"],
            "video_model": model_status["video_model"],
            "jobs": jobs,
            "counts": counts,
            "automation": runtime.automation.status(),
            "session": snapshot["session"],
            "app_user": user,
            "tenant_id": user_id,
            "data_scope": {
                "jobs": len(jobs),
                "accounts": int(account_state.get("count") or 0),
                "challenge_accounts": len(snapshot.get("challenge_acceptances") or {}),
                "automation_accounts": len(
                    ((snapshot.get("automation") or {}).get("accounts") or {})
                ),
            },
        }

    @app.get("/api/hot-topics")
    def list_hot_topics(
        force: bool = False,
        _: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        hot_topics.fetch(force=force)
        return hot_topics.snapshot()

    def challenge_credentials(runtime: UserRuntime, account_id: str) -> dict[str, Any]:
        credentials = (
            runtime.accounts.credentials(account_id)
            if account_id
            else runtime.accounts.active_credentials()
        )
        if credentials is None:
            raise HTTPException(status_code=409, detail="请先添加并登录头条账号")
        return credentials

    def challenge_client(
        runtime: UserRuntime,
        account_id: str,
    ) -> ToutiaoChallengeClient:
        credentials = challenge_credentials(runtime, account_id)
        return ToutiaoChallengeClient(
            runtime.config,
            config_dir,
            cookie_override=credentials.get("cookies"),
            headers_override=credentials.get("headers"),
        )

    def attach_challenge_acceptances(
        runtime: UserRuntime,
        account_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        activities = payload.get("activities", [])
        acceptance_records = runtime.store.challenge_acceptance_records(account_id)
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            record = acceptance_records.get(str(activity.get("id") or ""), {})
            if (
                activity.get("repeat_mode") == "unknown"
                and isinstance(record, dict)
                and record.get("repeat_mode") in {"daily", "weekly", "once"}
            ):
                activity["repeat_mode"] = record["repeat_mode"]
                activity["repeat_reason"] = str(record.get("repeat_reason") or "")
                activity["daily_repeatable"] = record["repeat_mode"] == "daily"
                activity["weekly_repeatable"] = record["repeat_mode"] == "weekly"
        accepted_ids = runtime.store.accepted_challenge_ids(account_id, activities)
        for activity in activities:
            if isinstance(activity, dict):
                activity["accepted"] = str(activity.get("id") or "") in accepted_ids
        return payload

    @app.get("/api/challenges")
    def list_challenges(
        account_id: str = Query(default="", max_length=80),
        biz_id: int = Query(default=1, ge=1, le=2),
        part_status: int = Query(default=0, ge=0, le=2),
        category: str = Query(default="全部", max_length=40),
        query: str = Query(default="", max_length=120),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=100),
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            with challenge_client(runtime, account_id) as client:
                result = client.list(
                    biz_id=biz_id,
                    part_status=part_status,
                    category=category,
                    query=query,
                    page=page,
                    page_size=page_size,
                )
            return attach_challenge_acceptances(runtime, account_id, result)
        except LoginRequired as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProtocolChallenge as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublisherError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/challenges/{activity_id}")
    def get_challenge(
        activity_id: str,
        account_id: str = Query(default="", max_length=80),
        repeat_mode: str = Query(default="unknown", pattern="^(unknown|once|daily|weekly)$"),
        activity_url: str = Query(default="", max_length=2000),
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            with challenge_client(runtime, account_id) as client:
                detail = client.detail(activity_id, activity_url=activity_url)
            if detail.get("repeat_mode") == "unknown" and repeat_mode in {
                "once",
                "daily",
                "weekly",
            }:
                detail["repeat_mode"] = repeat_mode
                detail["daily_repeatable"] = repeat_mode == "daily"
                detail["weekly_repeatable"] = repeat_mode == "weekly"
            runtime.store.update_challenge_metadata(account_id, activity_id, detail)
            detail["accepted"] = activity_id in runtime.store.accepted_challenge_ids(
                account_id, [detail]
            )
            return detail
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LoginRequired as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProtocolChallenge as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublisherError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/challenges/accept-batch")
    def accept_challenge_batch(
        request: ChallengeBatchAcceptRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            with challenge_client(runtime, request.account_id) as client:
                result = client.list(
                    biz_id=request.biz_id,
                    part_status=request.part_status,
                    category=request.category,
                    query=request.query,
                    page=request.page,
                    page_size=request.page_size,
                    include_categories=False,
                )
                activities = [
                    activity
                    for activity in result.get("activities", [])
                    if isinstance(activity, dict)
                    and activity.get("status") == "active"
                ]
                for activity in activities:
                    client.enrich_activity(activity)
            accepted = runtime.store.accept_challenges(request.account_id, activities)
            return {
                **accepted,
                "accepted_ids": [str(item["activity_id"]) for item in accepted["records"]],
                "page": int(result.get("page") or request.page),
                "page_size": int(result.get("page_size") or request.page_size),
                "total": int(result.get("total") or 0),
                "scanned_count": len(result.get("activities", [])),
                "skipped_count": len(result.get("activities", [])) - len(activities),
            }
        except LoginRequired as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProtocolChallenge as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublisherError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/challenges/{activity_id}/accept")
    def accept_challenge(
        activity_id: str,
        request: ChallengeAcceptRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            with challenge_client(runtime, request.account_id) as client:
                detail = client.detail(activity_id, activity_url=request.activity_url)
            if detail.get("status") != "active":
                raise ValueError("该创作活动已结束")
            content_type = "video" if request.biz_id == 2 else "article"
            supported_types = {
                str(item.get("type"))
                for item in detail.get("publish_types", [])
                if isinstance(item, dict)
            }
            if content_type not in supported_types:
                label = "视频" if content_type == "video" else "图文"
                raise ValueError(f"该活动不支持{label}投稿")
            accepted = runtime.store.accept_challenges(
                request.account_id,
                [
                    {
                        "id": str(detail["id"]),
                        "title": str(detail.get("title") or "创作活动"),
                        "content_type": content_type,
                        "biz_id": request.biz_id,
                        "repeat_mode": request.repeat_mode
                        if request.repeat_mode != "unknown"
                        else detail.get("repeat_mode", "unknown"),
                        "repeat_reason": request.repeat_reason
                        or detail.get("repeat_reason", ""),
                        "detail_url": request.activity_url,
                    }
                ],
            )
            return {
                **accepted,
                "acceptance": accepted["records"][0],
                "platform_participated": bool(detail.get("participated")),
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LoginRequired as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProtocolChallenge as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublisherError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/challenges/{activity_id}/generate", status_code=202)
    def generate_challenge_content(
        activity_id: str,
        request: ChallengeGenerateRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            with challenge_client(runtime, request.account_id) as client:
                detail = client.detail(activity_id, activity_url=request.activity_url)
            if detail.get("status") != "active":
                raise ValueError("该创作活动已结束")
            publish_types = {
                str(item.get("type")): item
                for item in detail.get("publish_types", [])
                if isinstance(item, dict)
            }
            publish_type = publish_types.get(request.content_type)
            if publish_type is None:
                label = "视频" if request.content_type == "video" else "图文"
                raise ValueError(f"该活动不支持{label}投稿")
            guidance = str(detail.get("generation_guidance") or "")
            if request.introduction.strip():
                guidance = f"{guidance}\n\n【活动简介】\n{request.introduction.strip()}"
            runtime.store.accept_challenges(
                request.account_id,
                [
                    {
                        "id": str(detail["id"]),
                        "title": str(detail.get("title") or "头条创作活动"),
                        "introduction": request.introduction.strip(),
                        "content_type": request.content_type,
                        "biz_id": request.biz_id,
                        "repeat_mode": request.repeat_mode
                        if request.repeat_mode != "unknown"
                        else detail.get("repeat_mode", "unknown"),
                        "repeat_reason": request.repeat_reason
                        or detail.get("repeat_reason", ""),
                        "detail_url": request.activity_url,
                    }
                ],
            )
            return runtime.jobs.create(
                str(detail.get("title") or "头条创作活动"),
                guidance,
                request.word_count,
                request.auto_action,
                request.account_id,
                {
                    "id": str(detail["id"]),
                    "category": "创作活动",
                    "source": "头条创作活动",
                    "source_keys": ["toutiao-activity"],
                    "url": "",
                    "activity_id": str(publish_type.get("activity_tag") or detail["id"]),
                    "activity_title": str(detail.get("title") or ""),
                    "activity_forum_id": str(publish_type.get("forum_id") or ""),
                    "activity_content_type": request.content_type,
                },
                request.content_type,
                request.video_duration,
                request.video_aspect_ratio,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LoginRequired as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProtocolChallenge as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublisherError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/jobs", status_code=202)
    def create_job(
        request: GenerateRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            guidance = request.guidance.strip()
            if not guidance:
                try:
                    from hot_topics import suggest_writing_angle

                    guidance = suggest_writing_angle(
                        request.topic,
                        request.topic_category,
                        list(request.topic_source_keys or []),
                        0,
                        "",
                    )
                except Exception:
                    guidance = ""
            return runtime.jobs.create(
                request.topic,
                guidance,
                request.word_count,
                request.auto_action,
                request.account_id,
                {
                    "id": request.topic_id,
                    "category": request.topic_category,
                    "source": request.topic_source,
                    "source_keys": request.topic_source_keys,
                    "url": request.topic_url,
                },
                request.content_type,
                request.video_duration,
                request.video_aspect_ratio,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="目标账号不存在") from exc

    @app.get("/api/jobs/{job_id}/article")
    def get_article(
        job_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            return runtime.jobs.read_article(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/jobs/{job_id}/article")
    def update_article(
        job_id: str,
        request: DraftUpdate,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            return runtime.jobs.update_article(job_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/regenerate", status_code=202)
    def regenerate_job(
        job_id: str,
        request: RegenerateRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            return runtime.jobs.regenerate(job_id, request.target)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/publish", status_code=202)
    def publish_job(
        job_id: str,
        request: PublishRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            return runtime.jobs.publish(job_id, request.mode)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/jobs/{job_id}")
    def delete_job(
        job_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, bool]:
        if not runtime.store.delete_job(job_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"deleted": True}

    @app.get("/api/jobs/{job_id}/cover")
    def get_cover(
        job_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> FileResponse:
        job = runtime.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        cover = resolve_job_media_path(job, "cover", runtime)
        if cover is None:
            raise HTTPException(status_code=404, detail="封面尚未生成")
        return FileResponse(
            cover,
            media_type=guess_media_type(cover),
            filename=cover.name,
            content_disposition_type="inline",
        )

    @app.get("/api/jobs/{job_id}/video")
    def get_video(
        job_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> FileResponse:
        job = runtime.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        video = resolve_job_media_path(job, "video", runtime)
        if video is None:
            raise HTTPException(status_code=404, detail="视频尚未生成")
        return FileResponse(
            video,
            media_type=guess_media_type(video),
            filename=video.name,
            content_disposition_type="inline",
        )

    def check_protocol_session(runtime: UserRuntime) -> dict[str, Any]:
        try:
            credentials = runtime.accounts.active_credentials()
            with ToutiaoProtocolClient(
                runtime.config,
                config_dir,
                cookie_override=credentials.get("cookies") if credentials else None,
                headers_override=credentials.get("headers") if credentials else None,
            ) as client:
                result = client.check_session()
                profile = client.get_account_profile()
            if credentials:
                runtime.accounts.save_account(profile, credentials["cookies"], credentials["headers"])
            runtime.store.update_session("ready", "Cookie 会话检测通过")
            return result
        except LoginRequired as exc:
            runtime.store.update_session("error", str(exc))
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProtocolChallenge as exc:
            runtime.store.update_session("challenge", str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublisherError as exc:
            runtime.store.update_session("error", str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/session")
    def configure_session(
        request: SessionRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            with ToutiaoProtocolClient(
                runtime.config,
                config_dir,
                cookie_override=request.cookie,
                headers_override=request.headers,
            ) as client:
                result = client.check_session()
                profile = client.get_account_profile()
            account = runtime.accounts.save_account(profile, request.cookie, request.headers)
            runtime.store.update_session("ready", f"已导入账号：{account['name']}")
            return {"account": account, "check": result}
        except LoginRequired as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except PublisherError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/session/check")
    def check_session(runtime: UserRuntime = Depends(current_runtime)) -> dict[str, Any]:
        return check_protocol_session(runtime)

    @app.get("/api/accounts")
    def list_accounts(runtime: UserRuntime = Depends(current_runtime)) -> dict[str, Any]:
        return runtime.accounts.snapshot()

    @app.post("/api/accounts/{account_id}/activate")
    def activate_account(
        account_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            account = runtime.accounts.activate(account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="账号不存在") from exc
        runtime.store.update_session("ready", f"当前账号：{account['name']}")
        return account

    @app.patch("/api/accounts/{account_id}")
    def rename_account(
        account_id: str,
        request: AccountRenameRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            account = runtime.accounts.rename(account_id, request.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="账号不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if runtime.accounts.snapshot().get("active_id") == account_id:
            runtime.store.update_session("ready", f"当前账号：{account['name']}")
        return account

    @app.delete("/api/accounts/{account_id}")
    def delete_account(
        account_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, bool]:
        if not runtime.accounts.delete(account_id):
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"deleted": True}

    @app.post("/api/auth/qr")
    def start_qr_login(runtime: UserRuntime = Depends(current_runtime)) -> dict[str, Any]:
        try:
            return runtime.qr_login.start()
        except PublisherError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/auth/qr/{login_id}")
    def poll_qr_login(
        login_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            result = runtime.qr_login.poll(login_id)
            if result.get("status") == "confirmed":
                runtime.store.update_session("ready", f"登录成功：{result['account']['name']}")
            return result
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="登录二维码已失效") from exc
        except PublisherError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/models")
    def list_models(runtime: UserRuntime = Depends(current_runtime)) -> dict[str, Any]:
        return runtime.models.snapshot()

    @app.post("/api/models")
    def save_model(
        request: ModelProfileRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            return runtime.models.save(request.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/models/{profile_id}/activate")
    def activate_model(
        profile_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            return runtime.models.activate(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="模型配置不存在") from exc

    @app.delete("/api/models/{profile_id}")
    def delete_model(
        profile_id: str,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, bool]:
        if not runtime.models.delete(profile_id):
            raise HTTPException(status_code=409, detail="默认模型不可删除或配置不存在")
        return {"deleted": True}

    @app.post("/api/automation")
    def update_automation(
        request: AutomationRequest,
        runtime: UserRuntime = Depends(current_runtime),
    ) -> dict[str, Any]:
        try:
            return runtime.automation.update(
                request.account_id,
                request.enabled,
                request.interval_minutes,
                request.mode,
                request.pick_count,
                request.categories,
                request.sources,
                request.content_type,
                request.video_duration,
                request.auto_claim_challenges,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="目标账号不存在") from exc

    web_dir = config_dir / "web"
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="dashboard")
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--reload", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = args.config.expanduser().resolve()
    if not config_path.is_file() and config_path.name == "config.toml":
        example = config_path.with_name("config.example.toml")
        if example.is_file():
            config_path = example
    config = load_toml(config_path)
    dashboard = config.get("dashboard", {})
    host = args.host or str(dashboard.get("host", "127.0.0.1"))
    port = args.port or int(dashboard.get("port", 8765))
    uvicorn.run(create_app(config_path), host=host, port=port, reload=args.reload)


if __name__ == "__main__":
    main()
