"""Encrypted custom OpenAI-compatible model profiles."""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from toutiao_accounts import SecretBox, storage_paths
from db import load_or_migrate, save_store


class ModelProfileStore:
    def __init__(self, config: dict[str, Any], config_dir: Path) -> None:
        _, model_path, key_path = storage_paths(config, config_dir)
        self.base_config = config
        self.path = model_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.box = SecretBox(key_path)
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {
            "active": {
                "article": "builtin-article",
                "cover": "builtin-cover",
                "video": "builtin-video",
            },
            "profiles": [],
        }
        saved = load_or_migrate(self.path, kind="models")
        if isinstance(saved, dict):
            self.data["active"].update(saved.get("active", {}))
            self.data["profiles"] = saved.get("profiles", [])

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            profiles = self._builtins() + [self._public(item) for item in self.data["profiles"]]
            return {"active": dict(self.data["active"]), "profiles": profiles}

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        profile_id = str(payload.get("id") or f"model-{uuid.uuid4().hex[:12]}")
        if profile_id.startswith("builtin-"):
            raise ValueError("默认模型配置不可覆盖")
        with self.lock:
            existing = next((row for row in self.data["profiles"] if row.get("id") == profile_id), None)
            kind = str(payload["kind"])
            if existing and existing.get("kind") != kind:
                raise ValueError("模型用途不可修改，请新建配置")
            secret = (
                existing["secret"]
                if existing and not payload.get("api_key")
                else self.box.encrypt({"api_key": str(payload.get("api_key") or "")})
            )
            item = {
                "id": profile_id,
                "kind": kind,
                "name": str(payload["name"]),
                "base_url": str(payload["base_url"]).rstrip("/"),
                "model": str(payload["model"]),
                "temperature": float(payload.get("temperature", 0.7)),
                "json_mode": bool(payload.get("json_mode", True)),
                "size": str(payload.get("size", "1536x1024")),
                "quality": str(payload.get("quality", "medium")),
                "duration": int(payload.get("duration", 8)),
                "aspect_ratio": str(payload.get("aspect_ratio", "16:9")),
                "poll_interval": float(payload.get("poll_interval", 5)),
                "timeout": int(payload.get("timeout", 900)),
                "create_path": str(payload.get("create_path", "/videos")),
                "secret": secret,
            }
            if existing:
                self.data["profiles"] = [item if row.get("id") == profile_id else row for row in self.data["profiles"]]
            else:
                self.data["profiles"].append(item)
            self.data["active"][kind] = profile_id
            self._save()
            return self._public(item)

    def activate(self, profile_id: str) -> dict[str, Any]:
        profile = self._get(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        kind = str(profile["kind"])
        with self.lock:
            self.data["active"][kind] = profile_id
            self._save()
        return self._public(profile)

    def delete(self, profile_id: str) -> bool:
        if profile_id.startswith("builtin-"):
            return False
        with self.lock:
            existing = self._get(profile_id)
            if existing is None:
                return False
            self.data["profiles"] = [row for row in self.data["profiles"] if row.get("id") != profile_id]
            kind = str(existing["kind"])
            if self.data["active"].get(kind) == profile_id:
                self.data["active"][kind] = f"builtin-{kind}"
            self._save()
            return True

    def apply(self, config: dict[str, Any]) -> dict[str, Any]:
        effective = copy.deepcopy(config)
        for kind, section in (("article", "ai"), ("cover", "cover"), ("video", "video")):
            profile_id = str(self.data["active"].get(kind, f"builtin-{kind}"))
            if profile_id.startswith("builtin-"):
                continue
            profile = self._get(profile_id)
            if profile is None:
                continue
            secret = self.box.decrypt(str(profile["secret"]))
            target = effective.setdefault(section, {})
            target.update(
                {
                    "base_url": profile["base_url"],
                    "model": profile["model"],
                    "api_key": str(secret.get("api_key", "")),
                }
            )
            if kind == "article":
                target.update(
                    {"temperature": profile["temperature"], "json_mode": profile["json_mode"]}
                )
            elif kind == "cover":
                target.update({"size": profile["size"], "quality": profile["quality"]})
            else:
                target.update(
                    {
                        "size": profile["size"],
                        "duration": profile["duration"],
                        "aspect_ratio": profile["aspect_ratio"],
                        "poll_interval": profile["poll_interval"],
                        "timeout": profile["timeout"],
                        "create_path": profile["create_path"],
                    }
                )
        return effective

    def active_summary(self) -> dict[str, Any]:
        effective = self.apply(self.base_config)
        ai = effective.get("ai", {})
        cover = effective.get("cover", {})
        video = effective.get("video", {})
        key = str(ai.get("api_key") or os.getenv(str(ai.get("api_key_env", "OPENAI_API_KEY")), ""))
        return {
            "article_model": ai.get("model"),
            "cover_model": cover.get("model"),
            "video_model": video.get("model"),
            "api_key_configured": bool(key.strip()),
            "api_key_env": ai.get("api_key_env", "OPENAI_API_KEY"),
        }

    def _get(self, profile_id: str) -> dict[str, Any] | None:
        if profile_id == "builtin-article":
            return self._builtins()[0]
        if profile_id == "builtin-cover":
            return self._builtins()[1]
        if profile_id == "builtin-video":
            return self._builtins()[2]
        return next((row for row in self.data["profiles"] if row.get("id") == profile_id), None)

    def _builtins(self) -> list[dict[str, Any]]:
        ai = self.base_config.get("ai", {})
        cover = self.base_config.get("cover", {})
        video = self.base_config.get("video", {})
        return [
            {
                "id": "builtin-article",
                "kind": "article",
                "name": "默认文章模型",
                "base_url": ai.get("base_url", ""),
                "model": ai.get("model", ""),
                "temperature": ai.get("temperature", 0.7),
                "json_mode": ai.get("json_mode", True),
                "builtin": True,
                "api_key_configured": bool(
                    str(ai.get("api_key") or os.getenv(str(ai.get("api_key_env", "OPENAI_API_KEY")), "")).strip()
                ),
            },
            {
                "id": "builtin-cover",
                "kind": "cover",
                "name": "默认封面模型",
                "base_url": cover.get("base_url", ""),
                "model": cover.get("model", ""),
                "size": cover.get("size", "1536x1024"),
                "quality": cover.get("quality", "medium"),
                "builtin": True,
                "api_key_configured": bool(
                    str(
                        cover.get("api_key")
                        or os.getenv(
                            str(cover.get("api_key_env") or ai.get("api_key_env", "OPENAI_API_KEY")), ""
                        )
                    ).strip()
                ),
            },
            {
                "id": "builtin-video",
                "kind": "video",
                "name": "默认视频模型",
                "base_url": video.get("base_url", ""),
                "model": video.get("model", ""),
                "size": video.get("size", "1280x720"),
                "duration": video.get("duration", 8),
                "aspect_ratio": video.get("aspect_ratio", "16:9"),
                "poll_interval": video.get("poll_interval", 5),
                "timeout": video.get("timeout", 900),
                "create_path": video.get("create_path", "/videos"),
                "builtin": True,
                "api_key_configured": bool(
                    str(
                        video.get("api_key")
                        or os.getenv(str(video.get("api_key_env", "OPENAI_API_KEY")), "")
                    ).strip()
                ),
            },
        ]

    def _public(self, item: Mapping[str, Any]) -> dict[str, Any]:
        public = {key: value for key, value in item.items() if key != "secret"}
        if "secret" in item:
            public["api_key_configured"] = bool(self.box.decrypt(str(item["secret"])).get("api_key"))
        public.setdefault("builtin", False)
        return public

    def _save(self) -> None:
        save_store(self.path, self.data, kind="models", also_json=True)
